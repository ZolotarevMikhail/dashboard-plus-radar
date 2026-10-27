#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборщик среза цен WB из выгрузок v18 (прогретая вкладка браузера).

Когда прямой путь search.wb.ru блокирован (403/498 — штраф-режим по IP),
данные собираются из вкладки wildberries.ru через внутренний прокси сайта
(эндпоинт u-search …/v18/search) и сохраняются пачками на диск. Этот
скрипт разбирает такие пачки и строит те же три файла, что wb-prices.py:
  wb-my-products.json, wb-competitors.csv, data-snapshot.json

Правила сопоставления аналогов (is_analog, MY_SUPPLIER_IDS, MY_BRANDS,
MAX_SUPPLIERS) и сборка пар/файлов — общие с sbor-iz-brauzera.py
(берутся через exec, как в собрать-цены.py: переименовывать нельзя).

Запуск: python3 срез-браузера-v18.py радар-пачка-1.json радар-пачка-2.json ...
"""
import datetime
import json
import os
import sys

OUT = os.path.dirname(os.path.abspath(__file__))
SRC_DEFAULT = [
    "/Users/nadezda/claudecode/радар-пачка-1.json",
    "/Users/nadezda/claudecode/радар-пачка-2.json",
    "/Users/nadezda/claudecode/радар-пачка-3.json",
]


def load_brauzera_rules():
    """Взять is_analog/MY_SUPPLIER_IDS/MY_BRANDS/MAX_SUPPLIERS из
    sbor-iz-brauzera.py — единая точка истины для правил."""
    path = os.path.join(OUT, "sbor-iz-brauzera.py")
    with open(path, encoding="utf-8") as file:
        source = file.read()
    namespace = {"__file__": path}
    exec(compile(source, path, "exec"), namespace)
    return namespace


def to_card(raw):
    """Строка выдачи v18 → карточка (цены в выдаче уже в копейках)."""
    prices = ((raw.get("sizes") or [{}])[0].get("price") or {})
    basic, final = prices.get("basic") or 0, prices.get("product") or 0
    return {
        "nm_id": raw["id"],
        "brand": raw.get("brand", ""),
        "name": raw.get("name", ""),
        "supplier": raw.get("supplier", ""),
        "supplier_id": raw.get("supplierId"),
        "supplier_rating": raw.get("supplierRating"),
        "rating": raw.get("rating"),
        "feedbacks": raw.get("feedbacks", 0),
        "price_basic_rub": round(basic / 100) if basic else None,
        "price_final_rub": round(final / 100) if final else None,
        "discount_pct": (
            round((1 - final / basic) * 100, 1)
            if basic and final else None),
    }


def load_pool(paths):
    """Прочитать пачки выгрузок браузера, убрать дубликаты по nm_id.

    Файл — строка JSON (результат evaluate), внутри словарь
    «запрос → [страницы]», товар — формат v18 (products на верхнем
    уровне) или формат wb-disc-pool (rows).
    """
    items, seen = [], set()
    for path in paths:
        with open(path, encoding="utf-8") as file:
            loaded = json.load(file)
        if isinstance(loaded, str):
            loaded = json.loads(loaded)
        pages = []
        for _query, parts in loaded.items():
            pages.extend(parts)
        for part in pages:
            raw_products = part.get("products") or part.get("rows") or []
            for raw in raw_products:
                card = to_card(raw) if "sizes" in raw else raw
                if card.get("nm_id") not in seen:
                    seen.add(card["nm_id"])
                    items.append(card)
    return items


def main():
    paths = sys.argv[1:] or SRC_DEFAULT
    rules = load_brauzera_rules()
    items = load_pool(paths)
    mine = [
        product for product in items
        if rules["is_mine"](product)]
    competitors = [
        product for product in items
        if not rules["is_mine"](product)
        and (product.get("brand") or "").lower() not in rules["MY_BRANDS"]
        and product["price_final_rub"]]
    print(f"Карточек из выгрузок: {len(items)}, наших: {len(mine)}, "
          f"конкурентов с ценой: {len(competitors)}")
    if not mine:
        print("Наши товары не найдены — проверьте выгрузки/продавца.")
        sys.exit(1)
    # дата среза — по факту прогона (не подделываем свежесть)
    stamp = datetime.date.today().isoformat()
    rows, top, pairs_before = rules["build_rows"](mine, competitors)
    rules["save_files"](mine, rows, top, stamp)
    print(f"Наша карточка × конкурент: до лимита {pairs_before}, "
          f"после (топ-{rules['MAX_SUPPLIERS']}): {len(rows)}")
    print("Топ конкурентов:")
    for supplier, stats in top[:10]:
        print(f"  {supplier:42s} позиций: {len(stats['pos']):3d} "
              f"отзывов: {stats['fb']}")
    print(f"Готово {stamp}: файлы радара перезаписаны (старые — в "
          "снапшот-архив/)")


if __name__ == "__main__":
    main()
