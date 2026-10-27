#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Финальная сборка среза из выгрузки браузера (wb-disc-*.json):
пары «аналог — аналог» и файлы для дашборда.
Запуск: python3 sbor-iz-brauzera.py

ВАЖНО: собрать-цены.py берёт из этого файла правила сопоставления
(is_analog, MY_SUPPLIER_IDS, MY_BRANDS, MAX_SUPPLIERS) через exec —
эти имена нельзя переименовывать.
"""
import csv
import json
import os
import re

OUT = os.path.dirname(os.path.abspath(__file__))
SRC = ["/Users/nadezda/claudecode/wb-disc-pool.json",
       "/Users/nadezda/claudecode/wb-disc-pool2.json"]
MAX_SUPPLIERS = 10   # оставляем не более 10 самых крупных конкурентов
MY_SUPPLIER_IDS = {34140}
MY_BRANDS = {"sproots", "spr"}
WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|гр|г|грамм|килограмм)\b",
                       re.I)
JUNK = {
    "набор", "штук", "шт", "см", "кг", "мм",
    "для", "и", "в", "на", "с", "ко", "из", "of",
}


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
    # вес убираем ДО разбора на слова: «20кг» — это не подтверждение
    # аналогичности
    text = WEIGHT_RE.sub(" ", text)
    return {
        word.lower() for word
        in re.sub(r"[^\wа-яёА-ЯЁ]", " ", text).split()
        if len(word) > 2
    } - JUNK


def is_child(title):
    """Детский товар или нет («детский вес», «для детей»)."""
    text = title.lower()
    return "детск" in text or "детей" in text


def is_adjustable(title):
    """Регулируемый вес или нет («регулируемый утяжелитель 10–60 кг»)."""
    return "регулир" in title.lower()


def weight_range_g(text):
    """Диапазон веса: «10–60 кг» → (10000, 60000);
    одиночный вес «0,5 кг» → (500, 500). Без веса — None."""
    text = text.lower()
    match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*[–—-]\s*(\d+(?:[.,]\d+)?)"
        r"\s*(кг|гр|г|грамм|килограмм)\b", text)
    if match:
        low = float(match.group(1).replace(",", "."))
        high = float(match.group(2).replace(",", "."))
        if match.group(3).startswith("к"):
            low *= 1000
            high *= 1000
        return (round(min(low, high)), round(max(low, high)))
    grams = weight_g(text)
    return (grams, grams) if grams is not None else None


def has_weight(title):
    """Есть ли в названии вес вообще (у регулируемого — диапазон вроде
    «10–60 кг»): отличает регулируемый ВЕС от «регулируемого» чего-то
    другого (например, сопротивления)."""
    return re.search(
        r"\d+(?:[.,]\d+)?\s*(кг|гр|г|грамм|килограмм)\b", title.lower()
    ) is not None


def is_analog(title_a, title_b):
    """Аналоги или нет — правила разбора двух названий."""
    # 1) «детский» сравнивается только с «детским»;
    #    детских аналогов нет — пары нет
    if is_child(title_a) != is_child(title_b):
        return False
    # 2) регулируемый вес — отдельная группа: не сравнивается
    #    с фиксированным весом и «без веса»
    if is_adjustable(title_a) != is_adjustable(title_b):
        return False
    # 3) жилет «без грузов» — пустой, его цена не сравнима
    #    с жилетом с грузами
    if ("без груз" in title_a.lower()) != ("без груз" in title_b.lower()):
        return False
    common_words = tokens(title_a) & tokens(title_b)
    if is_adjustable(title_a):
        # оба регулируемые: у обоих в названии должен быть вес (иначе
        # «регулируемый пружинный» спарится с «регулируемым 10–60 кг»),
        # и диапазоны весов должны пересекаться — «10 кг» не спарится
        # с «0,5 кг» (это разные товары)
        if has_weight(title_a) != has_weight(title_b):
            return False
        range_a, range_b = weight_range_g(title_a), weight_range_g(title_b)
        if range_a and range_b:
            return (
                range_a[0] <= range_b[1]
                and range_b[0] <= range_a[1]
                and len(common_words) >= 2
            )
        return len(common_words) >= 2
    weight_a, weight_b = weight_g(title_a), weight_g(title_b)
    if weight_a is None and weight_b is None:
        # веса нет ни у одного — аналогичность по словам типа товара
        # (минимум 2)
        return len(common_words) >= 2
    # 4) вес должен совпадать точно: 250 г сравнивается только с 250 г
    #    (у одного вес не указан — сверить нельзя, тоже не пара)
    #    плюс минимум 2 общих слова типа товара — иначе «гири 20 кг»
    #    спарится с «сэндбэгом 20 кг»
    return (
        weight_a is not None
        and weight_b is not None
        and weight_a == weight_b
        and len(common_words) >= 2
    )


def is_mine(product):
    # наши карточки = только наш продавец (34140). Карточки бренда SPR
    # у перекупов — не наши: их цены не должны попадать ни в «наши»,
    # ни в «конкурентов».
    return product.get("supplier_id") in MY_SUPPLIER_IDS


def to_card(raw_product):
    """Привести строку выгрузки браузера к карточке дашборда."""
    return {
        "nm_id": raw_product["id"],
        "brand": raw_product.get("brand", ""),
        "name": raw_product.get("name", ""),
        "supplier": raw_product.get("sup", ""),
        "supplier_id": raw_product.get("sid"),
        "rating": raw_product.get("rating"),
        "feedbacks": raw_product.get("fb", 0),
        "price_basic_rub": (
            round(raw_product["b"] / 100) if raw_product.get("b") else None),
        "price_final_rub": (
            round(raw_product["f"] / 100) if raw_product.get("f") else None),
        "discount_pct": (
            round((1 - raw_product["f"] / raw_product["b"]) * 100, 1)
            if raw_product.get("b") and raw_product.get("f") else None),
    }


def load_items():
    """Прочитать выгрузки браузера: [{...}] по каждому запросу
    (файл wb-disc-pool*.json), убрать дубликаты по nm_id."""
    items, seen_ids = [], set()
    for path in SRC:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
        for part in data:
            for raw_product in part.get("rows", []):
                card = to_card(raw_product)
                if card["nm_id"] not in seen_ids:
                    seen_ids.add(card["nm_id"])
                    items.append(card)
    return items


def build_rows(mine, competitors):
    """Пары «наш товар × конкурент-аналог» и сводка по конкурентам."""
    rows, seen_pairs = [], set()
    comp_index = {}
    for my_card in mine:
        for comp_card in competitors:
            pair = (my_card["nm_id"], comp_card["nm_id"])
            if pair in seen_pairs:
                continue
            if not is_analog(my_card["name"], comp_card["name"]):
                continue
            seen_pairs.add(pair)
            rows.append({
                "my_nm": my_card["nm_id"],
                "my_name": my_card["name"],
                "my_price": my_card["price_final_rub"],
                "comp_supplier": comp_card["supplier"],
                "comp_nm": comp_card["nm_id"],
                "comp_name": comp_card["name"],
                "comp_price": comp_card["price_final_rub"],
                "comp_discount_pct": comp_card["discount_pct"],
                "comp_rating": comp_card["rating"],
                "comp_feedbacks": comp_card["feedbacks"],
            })
            entry = comp_index.setdefault(
                comp_card["supplier"], {"pos": set(), "fb": 0})
            entry["pos"].add(comp_card["nm_id"])
            entry["fb"] += comp_card["feedbacks"] or 0

    # «крупность» конкурента: сначала отзывы (реальные продажи),
    # затем число позиций
    ranked = sorted(
        comp_index.items(),
        key=lambda kv: (kv[1]["fb"], len(kv[1]["pos"])),
        reverse=True,
    )
    top = ranked[:MAX_SUPPLIERS]
    top_suppliers = {supplier for supplier, _ in top}
    pairs_before_limit = len(rows)
    rows = [row for row in rows if row["comp_supplier"] in top_suppliers]
    return rows, top, pairs_before_limit


def save_files(mine, rows, top, stamp=None):
    """Записать wb-competitors.csv, data-snapshot.json,
    wb-my-products.json."""
    if stamp is None:
        stamp = "2026-08-31 (браузерный сбор)"
    with open(os.path.join(OUT, "wb-competitors.csv"), "w",
              encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, delimiter=";", fieldnames=[
            "my_nm", "my_name", "my_price", "comp_supplier",
            "comp_nm", "comp_name", "comp_price",
            "comp_discount_pct", "comp_rating", "comp_feedbacks",
        ])
        writer.writeheader()
        writer.writerows(rows)

    with open(os.path.join(OUT, "data-snapshot.json"), "w",
              encoding="utf-8") as file:
        json.dump({
            "snapshot": stamp,
            "top_suppliers": [
                {
                    "supplier": supplier,
                    "positions": len(stats["pos"]),
                    "feedbacks": stats["fb"],
                }
                for supplier, stats in top
            ],
            "rows": rows,
        }, file, ensure_ascii=False, indent=1)

    with open(os.path.join(OUT, "wb-my-products.json"), "w",
              encoding="utf-8") as file:
        json.dump({"snapshot": stamp, "items": mine}, file,
                  ensure_ascii=False, indent=1)


def main():
    items = load_items()
    mine = [product for product in items if is_mine(product)]
    print(f"Товаров всего: {len(items)}, наших: {len(mine)}")

    # конкуренты: не наши по продавцу И не карточки нашего бренда
    # (перекупы SPR по завышенной цене — не конкуренты)
    competitors = [
        product for product in items
        if not is_mine(product)
        and (product.get("brand") or "").lower() not in MY_BRANDS
        and product["price_final_rub"]
    ]
    rows, top, pairs_before_limit = build_rows(mine, competitors)
    save_files(mine, rows, top)

    print(f"Пар до лимита конкурентов: {pairs_before_limit}; "
          f"после (топ-{MAX_SUPPLIERS}): {len(rows)}")
    print("Топ конкурентов:")
    for supplier, stats in top:
        positions = len(stats["pos"])
        feedbacks = stats["fb"]
        print(f"  {supplier:42s} позиций: {positions:3d} "
              f"отзывов: {feedbacks}")
    print("Файлы записаны: wb-competitors.csv, data-snapshot.json, "
          "wb-my-products.json")


if __name__ == "__main__":
    main()
