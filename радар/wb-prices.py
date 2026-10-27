#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборщик среза цен WB — версия 3 (двухступенчатая, опыт Dashboard-SPR
и статьи парсеров).

WB жёстко лимитирует ТОЛЬКО поисковый сервер (search.wb.ru). Сервер карточек
(card.wb.ru/v4/detail) не лимитируется и отдаёт до 100 товаров одним запросом.

Схема:
  1) Поиск — минимум запросов, только чтобы найти ID товаров
     (наши + конкуренты).
  2) Цены и всё остальное — пачками по 100 ID с сервера карточек.
  3) Пары «аналог — аналог» — локально, без сети.

Запуск: python3 wb-prices.py
Файлы: wb-my-products.json, wb-competitors.csv, data-snapshot.json
"""
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

SEARCH = "https://search.wb.ru/exactmatch/ru/common/v5/search"
CARDS = "https://card.wb.ru/cards/v4/detail"

# Поиск по бренду (ловим свои товары)
# + по категориям ассортимента (ловим конкурентов).
QUERIES = [
    ("sproots", 3),
    ("утяжелители для ног и рук", 1),
    ("жилет утяжелитель", 1),
    ("сэндбэг", 1),
    ("гиря", 1),
    ("мяч для бокса", 1),
    ("эспандер бойца", 1),
    ("тренажёр для шеи", 1),
    ("тормозной парашют плавания", 1),
    ("термо костюм сауна", 1),
    ("пояс атлетический", 1),
]
QUERY_BY_Q = dict(QUERIES)

# macOS-пакет Python без корневых сертификатов: фолбэк для публичного GET.
SSL_CTX = ssl.create_default_context()
try:
    with urllib.request.urlopen("https://search.wb.ru", timeout=5,
                                context=SSL_CTX):
        pass
except urllib.error.URLError as url_err:
    if "certificate" in str(getattr(url_err, "reason", url_err)).lower():
        SSL_CTX = ssl._create_unverified_context()
except Exception:
    pass


def _open(request):
    return urllib.request.urlopen(request, timeout=25, context=SSL_CTX)


# Origin/Referer как у настоящего сайта:
# без них WB чаще отдаёт 429/антибот-страницу.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
    "Accept": "application/json",
}
MY_SUPPLIER_IDS = {34140}
MY_BRANDS = {"sproots", "spr"}

WEIGHT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(кг|гр|г|грамм|килограмм)\b", re.I)
JUNK_WORDS = {
    "набор", "штук", "шт", "см", "кг", "мм",
    "для", "и", "в", "на", "с", "ко", "из", "of",
}


def fetch_json(url):
    """Скачать JSON с автоповтором: долгие паузы при 429, короткие иначе."""
    request = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(4):
        try:
            with _open(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as fetch_err:
            print(
                f"  ! попытка {attempt + 1}: {fetch_err}", file=sys.stderr)
            if "429" in str(fetch_err):
                pause = (15, 30, 60, 90)[attempt]
            else:
                pause = 3 + attempt * 3
            time.sleep(pause)
    return None


def valid_products(data):
    """WB иногда подменяет ответ: форма другая, но 200. Проверяем форму
    ответа, а не только HTTP-статус."""
    return isinstance(data, dict) and isinstance(data.get("products"), list)


def parse_products(data):
    """Достать из ответа товары с ценой (цены приходят в копейках)."""
    products = []
    for product in data["products"]:
        size = (product.get("sizes") or [{}])[0]
        price = size.get("price") or {}
        basic = price.get("basic") or 0
        final = price.get("product") or 0
        if basic and final and basic > 0:
            discount = round((1 - final / basic) * 100, 1)
        else:
            discount = None
        products.append({
            "nm_id": product.get("id"),
            "brand": product.get("brand", ""),
            "name": product.get("name", ""),
            "supplier": product.get("supplier", ""),
            "supplier_id": product.get("supplierId"),
            "supplier_rating": product.get("supplierRating"),
            "rating": product.get("rating"),
            "feedbacks": product.get("feedbacks", 0),
            "price_basic_rub": round(basic / 100) if basic else None,
            "price_final_rub": round(final / 100) if final else None,
            "discount_pct": discount,
        })
    return products


def discover_ids():
    """Ступень 1: поиск — только ID и категория запроса.
    Страницы глубже 12 WB душит."""
    found = {}   # nm_id -> запрос ("категория")
    for query_text, pages in QUERIES:
        for page in range(1, pages + 1):
            query_string = urllib.parse.urlencode({
                "ab_testing": "false", "appType": "1", "curr": "rub",
                "dest": "-1257786", "query": query_text,
                "resultset": "catalog", "sort": "popular", "spp": "30",
                "page": str(page),
            })
            batch = []
            data = fetch_json(f"{SEARCH}?{query_string}")
            if valid_products(data):
                batch = parse_products(data)
            print(f"  '{query_text}' стр.{page}: {len(batch)} товаров")
            for item in batch:
                if item["nm_id"] not in found:
                    found[item["nm_id"]] = query_text
            time.sleep(2.5)
    return found


def fetch_cards(nm_ids, chunk=100):
    """Ступень 2: цены/рейтинги пачками с нелимитируемого сервера карточек."""
    products = []
    for start in range(0, len(nm_ids), chunk):
        part = nm_ids[start:start + chunk]
        query_string = urllib.parse.urlencode({
            "appType": "1", "curr": "rub", "dest": "-1257786",
            "nm": ";".join(str(nm_id) for nm_id in part),
        })
        data = fetch_json(f"{CARDS}?{query_string}")
        if valid_products(data):
            got_ids = {p.get("id"): p for p in data["products"]}
            missing = [nm_id for nm_id in part if nm_id not in got_ids]
            products += parse_products(data)
            tail = ""
            if missing:
                tail = f", нет в ответе: {len(missing)}"
            print(f"  карточки {start + 1}–{start + len(part)}: "
                  f"+{len(got_ids)}{tail}")
        else:
            print(f"  карточки {start + 1}–{start + len(part)}: "
                  "пустой/битый ответ")
        time.sleep(1.2)
    return products


def weight_g(text):
    """Вес в граммах из названия («300 г», «2 кг») — или None."""
    matches = list(WEIGHT_RE.finditer(text.lower()))
    if not matches:
        return None
    grams = float(matches[0].group(1).replace(",", "."))
    if matches[0].group(2).lower().startswith("к"):
        grams *= 1000
    return round(grams)


def tokens(text):
    return {
        word.lower() for word
        in re.sub(r"[^\wа-яёА-ЯЁ]", " ", text).split()
        if len(word) > 2
    } - JUNK_WORDS


def is_analog(my_name, comp_name):
    """Аналоги: либо совпадает вес (±10%), либо много общих слов."""
    my_weight, comp_weight = weight_g(my_name), weight_g(comp_name)
    if my_weight and comp_weight:
        ratio = abs(my_weight - comp_weight) / max(my_weight, comp_weight)
        return ratio <= 0.10
    common = tokens(my_name) & tokens(comp_name)
    return len(common) >= 5


def is_mine(product):
    return (
        product["supplier_id"] in MY_SUPPLIER_IDS
        or (product["brand"] or "").lower() in MY_BRANDS
    )


def build_competitor_report(found, items):
    """Ступень 3: пары «мой товар × конкурент-аналог»
    + сводка конкурентов."""
    rows, seen_pairs = [], set()
    comp_index = {}
    for query_text in QUERY_BY_Q:
        bucket = [nm_id for nm_id in found if found[nm_id] == query_text]
        bucket_ids = set(bucket)
        query_items = [
            product for product in items if product["nm_id"] in bucket_ids]
        query_mine = [
            product for product in query_items if is_mine(product)]
        query_comp = [
            product for product in query_items
            if not is_mine(product) and product["price_final_rub"]]
        for my_product in query_mine:
            for comp_product in query_comp:
                pair = (my_product["nm_id"], comp_product["nm_id"])
                if pair in seen_pairs:
                    continue
                if not is_analog(my_product["name"],
                                 comp_product["name"]):
                    continue
                seen_pairs.add(pair)
                rows.append({
                    "my_nm": my_product["nm_id"],
                    "my_name": my_product["name"],
                    "my_price": my_product["price_final_rub"],
                    "comp_supplier": comp_product["supplier"],
                    "comp_nm": comp_product["nm_id"],
                    "comp_name": comp_product["name"],
                    "comp_price": comp_product["price_final_rub"],
                    "comp_discount_pct": comp_product["discount_pct"],
                    "comp_rating": comp_product["rating"],
                    "comp_feedbacks": comp_product["feedbacks"],
                    "comp_supplier_rating":
                        comp_product["supplier_rating"],
                })
                entry = comp_index.setdefault(
                    comp_product["supplier"],
                    {"positions": set(), "feedbacks": 0})
                entry["positions"].add(comp_product["nm_id"])
                entry["feedbacks"] += comp_product["feedbacks"] or 0

    top_comp = sorted(
        comp_index.items(),
        key=lambda kv: (len(kv[1]["positions"]), kv[1]["feedbacks"]),
        reverse=True,
    )[:12]
    return rows, top_comp


def save_report(out_dir, timestamp, rows, top_comp):
    """Записать два файла отчёта: таблицу конкурентов и снапшот."""
    with open(os.path.join(out_dir, "wb-competitors.csv"), "w",
              encoding="utf-8-sig", newline="") as file:
        fieldnames = list(rows[0].keys()) if rows else ["my_nm"]
        writer = csv.DictWriter(file, delimiter=";", fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with open(os.path.join(out_dir, "data-snapshot.json"), "w",
              encoding="utf-8") as file:
        json.dump({
            "snapshot": timestamp,
            "top_suppliers": [
                {
                    "supplier": supplier,
                    "positions": len(stats["positions"]),
                    "feedbacks": stats["feedbacks"],
                }
                for supplier, stats in top_comp
            ],
            "rows": rows,
        }, file, ensure_ascii=False, indent=1)


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    print("=== Ступень 1. Поиск: собираем ID (наши + конкуренты) ===")
    found = discover_ids()
    print(f"Уникальных ID: {len(found)}")

    print("=== Ступень 2. Карточки: цены без лимитов ===")
    items = fetch_cards(list(found.keys()))
    mine = [product for product in items if is_mine(product)]
    print(f"Получено карточек с ценами: {len(items)}, "
          f"из них SPROOTS: {len(mine)}")
    if not mine:
        print("Наши товары не найдены — "
              "проверь бренд/продавца в MY_SUPPLIER_IDS.")
        if not items:
            print("Выдача пуста и запросы блокируются (403/498) — "
                  "запасной маршрут: прогретая вкладка wildberries.ru "
                  "(эндпоинт u-search v18) + python3 срез-браузера-v18.py "
                  "<файлы-пачек.json>. Подробнее: память проекта про "
                  "wb-open-search-via-site-proxy.")
        sys.exit(1)

    with open(os.path.join(out_dir, "wb-my-products.json"), "w",
              encoding="utf-8") as file:
        json.dump({"snapshot": timestamp, "items": mine}, file,
                  ensure_ascii=False, indent=1)

    print("=== Ступень 3. Сопоставление аналогов (локально) ===")
    rows, top_comp = build_competitor_report(found, items)

    save_report(out_dir, timestamp, rows, top_comp)

    print(f"Сопоставлений (мой товар × конкурент-аналог): {len(rows)}")
    print("Топ конкурентов:")
    for supplier, stats in top_comp[:10]:
        positions = len(stats["positions"])
        feedbacks = stats["feedbacks"]
        print(f"  {supplier:42s} позиций: {positions:3d} "
              f"отзывов(сумма): {feedbacks}")
    print(f"\nГотово {timestamp}: wb-my-products.json, "
          "wb-competitors.csv, data-snapshot.json")


if __name__ == "__main__":
    main()
