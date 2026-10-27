#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автоматический радар цен WB — раз в 3 дня (launchd, охрана расписания).

Что делает за прогон:
  1) Охрана расписания: свежий срез моложе 3 дней — тихий выход
     (launchd будит скрипт по календарю, срез собирать не надо).
  2) Playwright (настоящий Chrome с постоянным профилем «профиль-радара»,
     headless): открыть wildberries.ru, при антиботе дождаться его
     отсчёта (cookie антибота живёт в профиле — дальше прогревов не
     нужно), прогреть вкладку НАСТОЯЩИМ поиском и снять заголовки
     (deviceid, x-spa-version и прочие) из настоящего запроса сайта.
  3) Выдача по запросам — через внутренний прокси сайта (v18), между
     запросами паузы; при 498 (антибот) — пауза 40–70 с и снова.
  4) Правила сопоставления аналогов — из sbor-iz-brauzera.py (единая
     точка истины; подключается через exec — переименовывать нельзя).
  5) Файлы: старые копируются в снапшот-архив/, затем перезаписываются
     wb-my-products.json / wb-competitors.csv / data-snapshot.json;
     копия data-snapshot.json кладётся в папку дашборда Sproots-Наблюдатель.
  6) Список запросов — из wb-prices.py (тоже через exec, чтобы список
     жил в одном месте).

Прямой путь search.wb.ru в октябре стабильно 403 (штраф по IP), поэтому
сбор — только через браузерную вкладку (рецепт памяти
wb-open-search-via-site-proxy).

Запуск: python3 радар-сбор.py [--форс]
  --форс  — игнорировать охрану «моложе 3 дней» (разовый/проверочный прогон)
launchd: RunAtLoad + StartCalendarInterval 9:00 ежедневно — охрана сама
решает, пора ли срезу (раз в 3 дня).
"""
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

OUT = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(OUT, "sbor-iz-brauzera.py")
PRICES_PATH = os.path.join(OUT, "wb-prices.py")
ARCHIVE_DIR = os.path.join(OUT, "снапшот-архив")
POOL_DIR = os.path.join(OUT, "пачки")
DASH_DIR = os.path.join(os.path.dirname(OUT), "Sproots-Наблюдатель")
STATE_PATH = os.path.join(OUT, "состояние-радара.json")
LOG_PATH = os.path.join(OUT, "радар-журнал.txt")

FILES_NAMES = ["wb-my-products.json", "wb-competitors.csv",
               "data-snapshot.json"]
INTERVAL_DAYS = 3
START_URL = "https://www.wildberries.ru"
PROFILE_DIR = os.path.join(OUT, "профиль-радара")
CHROME_ARGS = ["--disable-blink-features=AutomationControlled"]
# UA фиксируем: антибот-токен в профиле привязан к строке User-Agent —
# прогрелся с ней, с ней и работаем (см. радар-журнал 26.10).
UA_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PAUSE_QUERY = (25, 40)          # пауза между запросами, с
PAUSE_498 = (40, 70)            # пауза при антиботе, с


def log(message):
    """Журнал радара: время + суть (почему собрал/пропустил/упал)."""
    with open(LOG_PATH, "a", encoding="utf-8") as file:
        file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def load_state():
    """Метка последнего успешного среза (для охраны расписания)."""
    try:
        with open(STATE_PATH, encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=1)


def due(days=INTERVAL_DAYS):
    """Срез готов к пересборке? True — если последний был старше N дней."""
    if "--форс" in sys.argv:
        return True
    last = load_state().get("last_ok", "")
    if not last:
        return True
    try:
        stamp = datetime.strptime(last[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return True
    return datetime.now() - stamp >= timedelta(days=days)


def exec_rules(path, extra_remove=None):
    """Подключить модуль без запуска его main(): exec с обрезкой.

    Так в проекте уже подключены правила (собрать-цены.py): имена
    (is_analog, MY_SUPPLIER_IDS и прочие) переименовывать нельзя.
    """
    with open(path, encoding="utf-8") as file:
        source = file.read()
    source = source.replace('if __name__ == "__main__":\n    main()', "")
    if extra_remove:
        source = source.replace(extra_remove, "")
    namespace = {"__file__": path}
    exec(compile(source, path, "exec"), namespace)
    return namespace


def wb_queries():
    """Список запросов выдачи — единая точка в wb-prices.py."""
    rules = exec_rules(PRICES_PATH)
    return rules["QUERIES"]


FJS = """async ([url, headers]) => {
    const resp = await fetch(url, {headers, credentials: 'include'});
    let body = null;
    try { body = await resp.json(); } catch (e) {}
    return {status: resp.status, body};
}"""


class WBTab:
    """Прогретая вкладка wildberries.ru: настоящие заголовки + fetch v18."""

    def __init__(self, playwright):
        self.context = playwright.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chrome", headless=True, locale="ru-RU",
            user_agent=UA_CHROME, args=CHROME_ARGS,
            viewport={"width": 1440, "height": 900})
        self.page = (self.context.pages[0] if self.context.pages
                     else self.context.new_page())
        self.base_url = None
        self.headers = {}

    def open_and_warm(self):
        """Открыть сайт; если антибот — дождаться его отсчёта (до ~4 мин:
        страница проходит сама, cookie антибота остаётся в профиле);
        вбить запрос в поисковую строку и поймать первый настоящий запрос
        к внутреннему поиску — из него шаблон URL и заголовки."""
        seen = []

        def capture(request):
            url = request.url
            if "__internal/u-search" in url and "query=" in url:
                seen.append({"url": url, "headers": dict(request.headers)})

        self.page.on("request", capture)
        self.page.goto(START_URL, wait_until="domcontentloaded",
                       timeout=60_000)
        for _ in range(30):                     # до ~4 мин на антибот-отсчёт
            if self.page.evaluate(
                    "() => !!document.querySelector('#searchInput')"):
                break
            self.page.wait_for_timeout(8000)
        else:
            raise RuntimeError(
                "поисковая строка не появилась за ~4 мин — антибот не "
                "пропустил (сеть/VPN)")
        self.page.fill("#searchInput", "sproots")
        self.page.keyboard.press("Enter")
        # VPN к WB медленный: внутренний запрос может уйти и за минуту
        for _ in range(240):                    # до 2 мин на первый поиск
            if seen:
                break
            time.sleep(0.5)
        if not seen:                            # второй заход: перезагрузка
            self.page.reload(wait_until="domcontentloaded", timeout=60_000)
            for attempt in range(30):
                if self.page.evaluate(
                        "() => !!document.querySelector('#searchInput')"):
                    break
                self.page.wait_for_timeout(4000)
            self.page.fill("#searchInput", "sproots")
            self.page.keyboard.press("Enter")
            for _ in range(240):
                if seen:
                    break
                time.sleep(0.5)
        if not seen:
            raise RuntimeError(f"внутренний поисковый запрос сайта не пойман "
                               f"(страница на {self.page.url[:60]})")
        first = seen[0]
        self.base_url = re.sub(r"(&page=\d+)", "", first["url"])
        self.headers = {key: value for key, value in first["headers"].items()
                        if key not in ("content-length", "content-type")}
        # x-queryid синтезируется на каждый запрос: qid<uid><время>
        query_id = self.headers.get("x-queryid", "")
        self.qid_prefix = query_id[:len(query_id) - 14] or "qid"

    def fresh_headers(self):
        """Заголовки с свежим x-queryid (текущее время, как на сайте)."""
        headers = dict(self.headers)
        headers["x-queryid"] = self.qid_prefix + datetime.now().strftime(
            "%Y%m%d%H%M%S")
        return headers

    def ui_search(self, query_text):
        """НАСТОЯЩИЙ поиск со страницы — лекарство от 498: сайт сам
        восстанавливает антибот-токен, после этого fetch снова живой."""
        try:
            self.page.fill("#searchInput", query_text)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(6000)
            log(f"  лечение 498: настоящий поиск '{query_text}' "
                "(токен сайта обновлён)")
        except Exception as exc:
            log(f"  настоящий поиск не удался: {exc}")

    def fetch_query(self, query_text):
        """Выдача одного запроса через внутренний прокси сайта.

        Возвращает список «сырых» товаров или None (антибот/битый ответ
        после попыток). Без параметра page — с ним WB бывает 403.
        При 498: настоящий поиск с страницы (обновляет токен) + пауза.
        """
        url = re.sub(r"(query=)[^&]*", r"\g<1>" + urllib.parse.quote(
            query_text), self.base_url)
        for attempt in range(3):
            out = self.page.evaluate(FJS, [url, self.fresh_headers()])
            body = (out or {}).get("body")
            ok_shape = isinstance(body, dict) \
                and isinstance(body.get("products"), list)
            if out and out["status"] == 200 and ok_shape:
                return body["products"]
            self.ui_search(query_text)
            pause = random.uniform(*PAUSE_498)
            log(f"  '{query_text}' попытка {attempt + 1}: статус "
                f"{out.get('status') if out else '?'} — пауза {int(pause)} с")
            time.sleep(pause)
        return None

    def close(self):
        self.context.close()


def to_card(raw_product):
    """Строка v18-выдачи → карточка правила (цены в копейках)."""
    prices = ((raw_product.get("sizes") or [{}])[0].get("price") or {})
    basic, final = prices.get("basic") or 0, prices.get("product") or 0
    return {
        "nm_id": raw_product.get("id"),
        "brand": raw_product.get("brand", ""),
        "name": raw_product.get("name", ""),
        "supplier": raw_product.get("supplier", ""),
        "supplier_id": raw_product.get("supplierId"),
        "rating": raw_product.get("rating"),
        "feedbacks": raw_product.get("feedbacks", 0),
        "price_basic_rub": round(basic / 100) if basic else None,
        "price_final_rub": round(final / 100) if final else None,
        "discount_pct": (
            round((1 - final / basic) * 100, 1)
            if basic and final else None),
    }


def collect(tab, queries):
    """Выдача по всем запросам с паузами: карточки без дубликатов.

    Запрос, на котором всё заблокировано, не останавливает сбор — но
    факт пишется в журнал.
    """
    items, seen = [], set()
    failed = []
    for index, (query_text, _pages) in enumerate(queries, start=1):
        raw_items = tab.fetch_query(query_text)
        if raw_items is None:
            failed.append(query_text)
            continue
        added = 0
        for raw in raw_items:
            card = to_card(raw)
            if card.get("nm_id") and card.get("price_final_rub") \
                    and card["nm_id"] not in seen:
                seen.add(card["nm_id"])
                items.append(card)
                added += 1
        log(f"  запрос {index}/{len(queries)} '{query_text}': "
            f"+{added} карточек")
        if index < len(queries):
            time.sleep(random.uniform(*PAUSE_QUERY))
    return items, failed


def archive_old():
    """Старые файлы радара — в снапшот-архив (копия, с датой в имени)."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    for name in FILES_NAMES:
        path = os.path.join(OUT, name)
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(ARCHIVE_DIR, f"{day}-{name}"))


def copy_to_dashboard():
    """Свежий срез — рядом с дашбордом (он читает data-snapshot.json
    из своей папки)."""
    target = os.path.join(DASH_DIR, "data-snapshot.json")
    shutil.copy2(os.path.join(OUT, "data-snapshot.json"), target)
    return target


def main():
    if not due():
        last = load_state().get("last_ok")
        log(f"пропуск: срез свежий (последний {last})")
        print(f"Срез свежий (последний {last}) — пересборка не нужна")
        return 0
    if "--форс" in sys.argv:
        log("прогон по флагу --форс (охрана расписания обойдена)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log(f"! playwright недоступен: {exc}")
        print("python-Playwright не найден (см. README проекта)",
              file=sys.stderr)
        return 1

    rules = exec_rules(RULES_PATH)
    queries = wb_queries()
    started = datetime.now()
    log("прогон начат ({} запросов)".format(len(queries)))
    try:
        tab = WBTab(sync_playwright().start())
        tab.open_and_warm()          # может упасть и по таймауту сайта
    except Exception as exc:
        log(f"! вкладка не прогрелась: {exc}")
        print("Не удалось прогреть вкладку wildberries.ru (сеть/антибот)",
              file=sys.stderr)
        return 1
    try:
        items, failed = collect(tab, queries)
    finally:
        tab.close()

    mine = [card for card in items if rules["is_mine"](card)]
    competitors = [
        card for card in items
        if not rules["is_mine"](card)
        and (card.get("brand") or "").lower() not in rules["MY_BRANDS"]
        and card["price_final_rub"]]
    ok_queries = len(queries) - len(failed)
    log(f"карточек {len(items)}, наших {len(mine)}, конкурентов "
        f"{len(competitors)}, запросов отработало {ok_queries}/{len(queries)}")
    # порог честности: отработало большинство запросов и наши товары
    # на месте — иначе срез неполный, старые файлы не перезаписываем
    if not mine or ok_queries * 100 < 60 * len(queries):
        log(f"! срез неполный ({ok_queries}/{len(queries)} запросов, "
            f"наших {len(mine)}) — файлы не перезаписаны")
        print(f"Антибот заблокировал {len(failed)} из {len(queries)} "
              "запросов — неполный срез записывать нельзя (см. журнал)",
              file=sys.stderr)
        return 1

    os.makedirs(POOL_DIR, exist_ok=True)
    with open(os.path.join(POOL_DIR, "выдача-" +
                           datetime.now().strftime("%Y-%m-%d") + ".json"),
              "w", encoding="utf-8") as file:
        json.dump({"запросов": len(queries), "не отдали": failed,
                   "карточек": items}, file, ensure_ascii=False, indent=1)

    stamp = datetime.now().strftime("%Y-%m-%d")
    archive_old()
    rows, top, pairs_before = rules["build_rows"](mine, competitors)
    rules["save_files"](mine, rows, top, stamp)

    copy_to_dashboard()
    state = load_state()
    state["last_ok"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_state(state)

    log(f"готово: пар до лимита {pairs_before}, "
        f"после топ-{rules['MAX_SUPPLIERS']} {len(rows)}; "
        f"срез {stamp} → и в папку дашборда")
    print(f"Срез готов за "
          f"{int((datetime.now() - started).total_seconds())} с: "
          f"карточек {len(items)}, наших {len(mine)}, пар "
          f"{len(rows)} (топ-{rules['MAX_SUPPLIERS']}); "
          f"копия в дашборд: {os.path.basename(copy_to_dashboard())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
