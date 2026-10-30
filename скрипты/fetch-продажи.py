#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневная выгрузка продаж Sproots по WB API (этап 4).

Схема работы:
  1) Ключ API берётся из .env рядом с проектом (WB_API_KEY) — в коде,
     чате и гите ключа нет.
  2) Выгрузка инкрементальная: каждый раз API отдаёт только строки,
     которых у нас ещё нет (по метке изменения lastChangeDate). Свежие
     строки дописываются в data-api/продажи-api.jsonl. Обрыв сети не
     теряет данные — следующий запуск добирает недостающее.
  3) Рядом пишется выгрузка-сводка по дням в формате бухгалтерского CSV
     (те же колонки) — дашборд читает её кнопкой «⤓ CSV».
  4) Сводка сверяется с бухгалтерией (двойная трасса): если расхождение
     больше 1% — это будет видно в дашборде как красная метка.
  5) Остатки берутся отдельным срезом (API остатков) в
     data-api/остатки.json.

Запуск: python3 fetch-продажи.py
(по расписанию: launchd запускает ежедневно в 9:00; частые вызовы
не страшны: скрипт молча выйдет, если сегодня уже выгружалось)
"""
import csv
import json
import os
import signal
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

# Проект-корень: в рабочей папке скрипт лежит в scripts/ (корень выше),
# в пакете заказчику — рядом с дашбордом (корень = папка скрипта).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ = (os.path.dirname(_SCRIPT_DIR)
        if os.path.basename(_SCRIPT_DIR) == "scripts" else _SCRIPT_DIR)
DATA_DIR = os.path.join(PROJ, "data-api")
ENV_PATH = os.path.join(PROJ, ".env")
JSONL_PATH = os.path.join(DATA_DIR, "продажи-api.jsonl")
CSV_PATH = os.path.join(PROJ, "продажи-из-api.csv")
MAP_PATH = os.path.join(PROJ, "карта-артикулов.json")
STATE_PATH = os.path.join(DATA_DIR, "состояние.json")
STOCK_PATH = os.path.join(DATA_DIR, "остатки-последние.json")
REPORTS_PATH = os.path.join(DATA_DIR, "отчёты-реализации.json")
LOG_PATH = os.path.join(DATA_DIR, "журнал-выгрузки.txt")

API_DOMAIN = "https://statistics-api.wildberries.ru"
ANALYTICS_DOMAIN = "https://seller-analytics-api.wildberries.ru"
ENDPOINT_SALES = API_DOMAIN + "/api/v1/supplier/sales"
ENDPOINT_ORDERS = API_DOMAIN + "/api/v1/supplier/orders"
# остатки-срез: старый /api/v1/supplier/stocks устарел (404) — новый
# метод seller-analytics (POST, найден через каталог MCP)
ENDPOINT_STOCKS = ANALYTICS_DOMAIN + "/api/analytics/v1/stocks-report/wb-warehouses"  # noqa: E501
# финансовые отчёты реализации: источник ТОЧНОЙ комиссии и удержаний WB.
# Старый /api/v5/supplier/reportDetailByPeriod сам отвечает 404
# «deprecated»; замена finance (POST, найдена через каталог MCP).
ENDPOINT_REPORTS = "https://finance-api.wildberries.ru" \
                   "/api/finance/v1/sales-reports/list"
# реклама: Спрутс — отдельный сервис WB, ключ тот же (проверено
# 29.10.2026: списком кампаний отвечает 200). Движок fullstats: ≤50
# кампаний и ≤31 день на запрос; в старом POST /adv/v2/fullstats — 404.
ADVERT_DOMAIN = "https://advert-api.wildberries.ru"
ENDPOINT_ADVERTS = ADVERT_DOMAIN + "/api/advert/v2/adverts"
ENDPOINT_FULLSTATS = ADVERT_DOMAIN + "/adv/v3/fullstats"
AD_PATH = os.path.join(DATA_DIR, "реклама-выгрузка.json")
AD_DAILY_PATH = os.path.join(DATA_DIR, "реклама-по-дням.json")
AD_TTL_HOURS = 20.0    # рекламные расходы не переписываются задним числом
# неполная выгрузка (был сбой сети) живёт куда меньше: её надо добирать
AD_TTL_INCOMPLETE_HOURS = 2.0
FULLSTATS_MAX_IDS = 50       # ограничение WB на запрос fullstats
FULLSTATS_MAX_DAYS = 31      # ограничение WB на диапазон fullstats
# будильник на любой запрос: соединение WB может «капать» байтами
# (зависание 29.10) — urlopen-таймаут ловит не всё
REQUEST_HARD_LIMIT = 120
AD_HARD_RETRIES = 2  # повтор при зависании, потом пачка сдана


def _stall_handler(signum, frame):
    """Сброс зависшего соединения: urlopen-таймаут ловит не всё."""
    raise TimeoutError(f"нет ответа WB за {REQUEST_HARD_LIMIT} с")


class Сторож:
    """Будильник вокруг запроса: срывает зависшее соединение TimeoutError.

    Использовать внутри try: зависание обрабатывается как обычная
    сетевая ошибка (автоповтор). Работает только в основном потоке —
    скрипт однопоточный.
    """

    def __enter__(self):
        self._old = signal.signal(signal.SIGALRM, _stall_handler)
        signal.alarm(REQUEST_HARD_LIMIT)

    def __exit__(self, exc_type, exc, tb):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._old)
        return False


# карточки (предмет/категория каждой позиции) — источник групп
# иерархии «тип → товар» в дашборде
ENDPOINT_CARDS = "https://content-api.wildberries.ru/content/v2/get/cards/list"

# macOS-пакет Python без корневых сертификатов: фолбэк для публичного GET.
# Тот же приём, что в радаре (wb-prices.py).
SSL_CTX = ssl.create_default_context()
try:
    with urllib.request.urlopen("https://statistics-api.wildberries.ru",
                                timeout=8, context=SSL_CTX):
        pass
except urllib.error.URLError as url_err:
    if "certificate" in str(getattr(url_err, "reason", url_err)).lower():
        SSL_CTX = ssl._create_unverified_context()
except Exception:
    pass

RETRY_PAUSES_429 = (15, 30, 60, 90)   # WB лимитирует жёстко: пауза длинная
RETRY_PAUSES_OTHER = (3, 6, 12, 24)
MAX_ATTEMPTS = 4


def load_token():
    """Ключ API из .env рядом с проектом (только имя файла в коде)."""
    if not os.path.isfile(ENV_PATH):
        return None
    with open(ENV_PATH, encoding="utf-8") as file:
        for line in file:
            if line.startswith("WB_API_KEY="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    return None


def load_state():
    """Состояние выгрузки: с какой даты продолжать, когда последний прогон."""
    try:
        with open(STATE_PATH, encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {"last_change": "2026-09-01T00:00:00", "last_ok_date": ""}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=1)


def log(message):
    """Журнал прогонов: время + суть (для разбора, почему выгрузка не шла)."""
    with open(LOG_PATH, "a", encoding="utf-8") as file:
        file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def api_get(url, params, token):
    """GET к WB API с автоповтором: при 429 — долгие паузы, иначе короткие.

    Возвращает список строк или None (после исчерпания попыток).
    Лимит статистики: 1 запрос в минуту на метод — в цикле повтора
    соблюдаем его автоматически (долгие паузы при 429).
    """
    query = "&".join(f"{key}={urllib.parse.quote(str(value))}"
                     for key, value in params.items())
    url = url + "?" + query
    for attempt in range(MAX_ATTEMPTS):
        request = urllib.request.Request(
            url, headers={"Authorization": token,
                          "Accept": "application/json"})
        try:
            with Сторож(), urllib.request.urlopen(request, timeout=60,
                                                  context=SSL_CTX) as reply:
                return json.loads(reply.read().decode("utf-8"))
        except Exception as exc:
            text = str(exc)
            log(f"  попытка {attempt + 1}: {text}")
            wait = (RETRY_PAUSES_429 if "429" in text
                    else RETRY_PAUSES_OTHER)[attempt]
            time.sleep(wait)
    return None


def row_type(row):
    """Тип строки выгрузки: «S» продажа/возврат, «O» заказ.

    Продажи и возвраты — строками метода sales (saleID начинается с
    S… или R… по документации WB), заказы — строками метода orders
    (у них есть поле isCancel). Метку пишем прямо в строку файла.
    """
    if "isCancel" in row:
        return "O"
    # и «S…», и «R…» — строка продажи/возврата (они различаются дальше
    # по префиксу saleID); метка нужна, чтобы отличить их от заказов
    return "S"


def write_jsonl(new_rows, seen_keys):
    """Дописывает только новые строки (ключ: srid+артикул+дата продажи)."""
    added = 0
    with open(JSONL_PATH, "a", encoding="utf-8") as file:
        for row in new_rows:
            key = (row.get("srid"), row.get("supplierArticle"),
                   row.get("date"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if "_t" not in row:
                row["_t"] = row_type(row)
            file.write(json.dumps(row, ensure_ascii=False,
                                  separators=(",", ":")) + "\n")
            added += 1
    return added


def read_existing_keys():
    """Прочитать ключи уже сохранённых строк (дедупликация между прогонами)."""
    seen = set()
    if os.path.isfile(JSONL_PATH):
        with open(JSONL_PATH, encoding="utf-8") as file:
            for line in file:
                try:
                    row = json.loads(line)
                    seen.add((row.get("srid"), row.get("supplierArticle"),
                              row.get("date")))
                except Exception:
                    continue
    return seen


def read_jsonl_rows():
    """Все строки файла продаж; оборванная строка пропускается с журналом.

    Обрыв посреди записи (прогон убит) не должен валить все будущие
    прогоны: битую строку пропускаем и помечаем в журнале. Потерянную
    строку WB отдаёт снова, если откатить метку (флаг --история).
    """
    rows = []
    bad = 0
    with open(JSONL_PATH, encoding="utf-8") as file:
        for line in file:
            try:
                rows.append(json.loads(line))
            except Exception:
                bad += 1
    if bad:
        log(f"! продажи-api.jsonl: оборванных строк {bad} — пропущено "
            f"(вернуть потерю: --история)")
    return rows


def mark_old_rows():
    """Помечает тип строк файла, сохранённого до метки «_t» (один раз).

    Строки-продажи/возвраты имеют saleID, строки-заказы — isCancel.
    Перезапись только если есть что пометить (каждый прогон файл не
    трогаем), и атомарно: сперва файл-рядом, потом перемещение —
    обрыв посреди записи не оставит файл полуразобранным.
    """
    if not os.path.isfile(JSONL_PATH):
        return
    with open(JSONL_PATH, encoding="utf-8") as file:
        lines = file.read().splitlines()
    changed = False
    new_lines = []
    for line in lines:
        try:
            row = json.loads(line)
            if "_t" not in row:
                row["_t"] = row_type(row)
                new_lines.append(json.dumps(row, ensure_ascii=False,
                                            separators=(",", ":")))
                changed = True
                continue
        except Exception:
            pass
        new_lines.append(line)
    if not changed:
        return
    handle, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(JSONL_PATH), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write("\n".join(new_lines) + "\n")
        os.replace(tmp_path, JSONL_PATH)
    except Exception:
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)
        raise


def aggregate_csv_rows(sales_rows, ad_daily=None):
    """Сводка по дням и артикулам в колонках бухгалтерского CSV.

    Правила (по документации WB):
      - продаётся = строка sales с saleID «S…»; возврат = «R…»;
      - выручка = finishedPrice («фактическая цена с учётом всех
        скидок»); возвраты вычитаются из выручки своего дня;
      - комиссия = priceWithDisc − forPay — упрощённая оценка (WB сам
        пишет расхождения с точным отчётом реализации); сверится в
        двойной трассе с бухгалтерией;
      - реклама — из API Спрутс (fetch_ads, файл «реклама-по-дням.json»):
        расход точно по nmId, приписывается артикулу, который у этого
        nmId виден в продажах; в дни без продаж по рекламируемому
        товару добавляется отдельная строка (расход без выручки).
    Строка без метки «_t» считается продажей (старый формат файла).
    """
    agg = defaultdict(lambda: defaultdict(float))
    nm_to_article = {}
    for row in sales_rows:
        nm_key = str(row.get("nmId") or "")
        article = row.get("supplierArticle", "")
        if nm_key and article and nm_key not in nm_to_article:
            nm_to_article[nm_key] = article
        day = (row.get("date") or "")[:10]
        is_return = (row.get("saleID") or "").startswith("R")
        bucket = agg[(day, article)]
        if row.get("_t") == "O":
            # Отменённая корзина не реальный заказ: isCancel=true,
            # иначе плитки «Заказы» и «Выкуп» лгут (знаменатель врёт).
            if row.get("isCancel"):
                continue
            bucket["orders"] += 1
            continue
        if is_return:
            bucket["returns"] += 1
            bucket["revenue"] -= row.get("finishedPrice") or 0
        else:
            bucket["qty"] += 1
            bucket["revenue"] += row.get("finishedPrice") or 0
            bucket["commission"] += (
                (row.get("priceWithDisc") or 0) - (row.get("forPay") or 0))
    if ad_daily:
        for day, per_nm in sorted(ad_daily.items()):
            if not day:
                continue
            for nm_key, spend in per_nm.items():
                # нулевой расход в день не создаёт строку: иначе в CSV
                # появляются тысячи строк «Реклама ₽ = 0» (шум, было 1938)
                if spend:
                    article = nm_to_article.get(nm_key, nm_key)
                    agg[(day, article)]["ad"] += spend
    csv_rows = []
    for (day, article), numbers in sorted(agg.items()):
        has_ad = "ad" in numbers
        csv_rows.append({
            "Дата": day, "Артикул": article,
            "Заказы шт": int(numbers["orders"]) or "",
            "Продано шт": int(numbers["qty"]),
            "Выручка ₽": round(numbers["revenue"]),
            "Комиссия ₽": round(numbers["commission"]),
            "Возвраты шт": int(numbers["returns"]) or "",
            "Реклама ₽": round(numbers["ad"]) if has_ad else "",
            "Остатки шт": "",
            "Себестоимость ₽/шт": "",
        })
    return csv_rows, len(agg)


def load_map():
    """Карта соответствия: числовой артикул API / nmId → внутренний SPR-….

    Карта собирается скриптом карта-артикулов.py (по названиям товаров).
    Ключами служат и артикулы WB (wb_articles), и nmId — рекламные
    расходы приходят от Спрутс именно по nmId, к тому же артикулу.
    Если файла нет или карта не грузится — артикулы остаются числовыми.
    """
    try:
        with open(MAP_PATH, encoding="utf-8") as file:
            cards = json.load(file).get("соответствия", [])
        translation = {}
        for card in cards:
            spr = card.get("spr")
            if not spr:
                continue
            targets = [card.get("nm_id")]
            targets += card.get("wb_articles") or []
            for article in targets:
                if article:
                    translation[str(article)] = spr
        return translation
    except Exception as exc:
        log(f"! карта артикулов не прочитана ({exc}) — артикулы числовые")
        return {}


def write_csv(csv_rows, translation=None):
    """Сводка по дням — файл, который читает дашборд кнопкой «⤓ CSV».

    Числовые артикулы API переводятся во внутренние SPR-… по карте
    (незамеченные остаются числовыми — честно, без выдумки).
    """
    for row in csv_rows:
        article = row["Артикул"]
        if translation:
            row["Артикул"] = translation.get(str(article), article)
    cols = ["Дата", "Артикул", "Заказы шт", "Продано шт", "Выручка ₽",
            "Комиссия ₽", "Возвраты шт", "Реклама ₽", "Остатки шт",
            "Себестоимость ₽/шт"]
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=cols, delimiter=";")
        writer.writeheader()
        writer.writerows(csv_rows)


def regenerate():
    """Пересборка сводки CSV из локального файла — без запросов к API.

    Нужно, когда карта артикулов обновилась или CSV потерялся: данные
    уже лежат в продажи-api.jsonl, повторный поход в API не нужен.
    """
    state = load_state()
    state["last_ok_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log("регенерация CSV из локального файла")
    rows = read_jsonl_rows()
    csv_rows, pairs = aggregate_csv_rows(rows, load_ad_daily())
    write_csv(csv_rows, load_map())
    log(f"готово (регенерация): строк {len(rows)}, "
        f"пары день×артикул {pairs}")
    print(f"CSV пересобран из локальных данных: пар день×артикул {pairs} "
          f"→ {os.path.basename(CSV_PATH)}")


def api_post(url, body, token):
    """POST к WB API с автоповтором (та же логика, что в api_get)."""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    for attempt in range(MAX_ATTEMPTS):
        request = urllib.request.Request(
            url, data=payload,
            headers={"Authorization": token,
                     "Content-Type": "application/json",
                     "Accept": "application/json"}, method="POST")
        try:
            with Сторож(), urllib.request.urlopen(request, timeout=60,
                                                  context=SSL_CTX) as reply:
                return json.loads(reply.read().decode("utf-8"))
        except Exception as exc:
            text = str(exc)
            log(f"  попытка {attempt + 1}: {text}")
            wait = (RETRY_PAUSES_429 if "429" in text
                    else RETRY_PAUSES_OTHER)[attempt]
            time.sleep(wait)
    return None


def fetch_stocks(token):
    """Срез остатков seller-analytics → data-api/остатки-последние.json.

    Возвращает {nmId: суммарное количество на складах}. Файл перезаписывается
    только когда структура ответа знакома (data.items): неожиданный ответ
    WB не должен затирать прежние остатки нулями.
    """
    body = {"dateFrom": datetime.now().strftime("%Y-%m-%d"),
            "dateTo": datetime.now().strftime("%Y-%m-%d")}
    answer = api_post(ENDPOINT_STOCKS, body, token)
    if answer is None:
        return None
    items = ((answer.get("data") or {}).get("items")
             if isinstance(answer, dict) else None)
    if items is None:
        log("! остатки: незнакомая структура ответа — прежние остатки "
            "сохранены, файл не перезаписан")
        return None
    by_nm = defaultdict(int)
    for item in items:
        by_nm[item.get("nmId")] += item.get("quantity") or 0
    with open(STOCK_PATH, "w", encoding="utf-8") as file:
        json.dump({"срез": datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "остатки": dict(by_nm)}, file, ensure_ascii=False,
                  indent=1)
    return by_nm


def fetch_reports(token):
    """Финансовые отчёты реализации → data-api/отчёты-реализации.json.

    Отчёты WB публикует понедельными кусочками с задержкой около
    недели; каждый содержит ТОЧНЫЕ суммы (продано, к перечислению,
    логистика, хранение, штрафы) — в отличие от упрощённой комиссии
    из выгрузки продаж. Период: от первой строки локальной выгрузки
    до сегодня, чтобы отчёты всегда покрывали весь интерес.
    Не критично для основного прогона: не пришли — журнал и дальше.
    """
    date_from = earliest_sale_day() or datetime.now().strftime("%Y-%m-%d")
    body = {"dateFrom": date_from,
            "dateTo": datetime.now().strftime("%Y-%m-%d")}
    answer = api_post(ENDPOINT_REPORTS, body, token)
    if answer is None:
        log("! отчёты реализации не получены (попытки исчерпаны)")
        return None
    with open(REPORTS_PATH, "w", encoding="utf-8") as file:
        json.dump({"срез": datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "период": f"{date_from}…{body['dateTo']}",
                   "отчёты": answer}, file, ensure_ascii=False, indent=1)
    log(f"отчёты реализации: {len(answer)} шт за {date_from}…{body['dateTo']}")
    return len(answer)


CARDS_TTL_DAYS = 7.0    # карточки меняются редко — кэш на неделю


def earliest_sale_day():
    """Самая ранняя дата локальной выгрузки («откуда данные начинаются»)."""
    earliest = ""
    if os.path.isfile(JSONL_PATH):
        with open(JSONL_PATH, encoding="utf-8") as file:
            for line in file:
                try:
                    day = (json.loads(line).get("date") or "")[:10]
                except Exception:
                    continue
                if day and (not earliest or day < earliest):
                    earliest = day
    return earliest


def ad_windows(begin, end):
    """Отрезки ≤ FULLSTATS_MAX_DAYS между begin и end (включительно)."""
    windows = []
    point = datetime.fromisoformat(begin)
    last = datetime.fromisoformat(end)
    while point <= last:
        piece = point + timedelta(days=FULLSTATS_MAX_DAYS - 1)
        if piece > last:
            piece = last
        windows.append((point.strftime("%Y-%m-%d"),
                        piece.strftime("%Y-%m-%d")))
        point = piece + timedelta(days=1)
    return windows


def ad_fullstats(token, ids_csv, begin, end):
    """Статистика кампаний fullstats за окно. Возвращает (ок, тело).

    ok False — сеть/исчерпаны попытки; тело None — WB вернул пустой
    ответ «null» (так отдаёт пачку, у которой нет данных за период).
    Частота жёсткая: соблюдаем заголовок X-RateLimit-Retry.
    """
    params = {"ids": ids_csv, "beginDate": begin, "endDate": end}
    query = "&".join(f"{key}={urllib.parse.quote(str(value))}"
                     for key, value in params.items())
    url = ENDPOINT_FULLSTATS + "?" + query
    hangs = 0
    for attempt in range(MAX_ATTEMPTS):
        request = urllib.request.Request(
            url, headers={"Authorization": token,
                          "Accept": "application/json"})
        try:
            with Сторож(), urllib.request.urlopen(request, timeout=60,
                                                  context=SSL_CTX) as reply:
                return True, json.loads(reply.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = wait_seconds(exc.headers.get("X-RateLimit-Retry"))
                log(f"  реклама 429: пауза {wait} с")
                time.sleep(wait)
                continue
            log(f"  реклама: ответ {exc.code} — пропускаю пачку")
            time.sleep(RETRY_PAUSES_OTHER[attempt])
        except TimeoutError as exc:
            hangs += 1
            if hangs >= AD_HARD_RETRIES:
                log(f"  реклама: соединение зависло дважды — пачка сдана "
                    f"({exc}); продолжится в следующий прогон")
                return False, None
            log(f"  реклама: зависание сброшено ({exc}), повтор")
            time.sleep(RETRY_PAUSES_OTHER[attempt])
        except Exception as exc:
            log(f"  реклама: {exc}")
            time.sleep(RETRY_PAUSES_OTHER[attempt])
    return False, None


def wait_seconds(value, fallback=60):
    """Пауза из заголовка X-RateLimit-Retry: число или запасной вариант."""
    try:
        wait = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(wait, 300) if wait > 0 else fallback


def ad_has_raw_entry(raw, ids, begin, end):
    """Есть ли в сырых окнах уже готовый ответ по этим кампаниям/дню."""
    for entry in raw:
        if (entry.get("beginDate") == begin and entry.get("endDate") == end
                and sorted(entry.get("ids") or []) == sorted(ids)):
            return True
    return False


def load_ad_raw():
    """Сырые окна рекламы из файла (для продолжения прерванного прогона)."""
    try:
        with open(AD_PATH, encoding="utf-8") as file:
            return json.load(file).get("окна") or []
    except Exception:
        return []


def save_ad_raw(raw):
    """Сырые окна → файл после КАЖДОГО запроса (програны продолжаемы)."""
    with open(AD_PATH, "w", encoding="utf-8") as file:
        json.dump({"срез": datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "окна": raw}, file, ensure_ascii=False, indent=1)


def ad_group_stats(token, ids, begin, end, raw):
    """Статистика пачки; на пустой пачке — одиночные запросы.

    Проверено 29.10.2026 живьём: WB отдаёт «null» на пачку из ≥2
    кампаний даже там, где у одиночек данные есть, — поэтому пачка
    дробится сразу до одиночных запросов. Каждый полученный ответ
    сразу в файл (raw) — прерванный прогон продолжается.
    Возвращает True, если пачка обработана (с данными или без).
    """
    if ad_has_raw_entry(raw, ids, begin, end):
        return True
    ok, body = ad_fullstats(token, ",".join(str(i) for i in ids), begin, end)
    if not ok:
        log(f"! реклама {begin}…{end}: пачка не получена (сеть)")
        return False
    if body is None:
        if len(ids) == 1:
            return True                     # честный «нет данных»
        log(f"  реклама: пачка {len(ids)} пустая — запросы по одиночным")
        for one in ids:
            if not ad_group_stats(token, [one], begin, end, raw):
                return False
        return True
    raw.append({"ids": list(ids), "beginDate": begin, "endDate": end,
                "кампании": body})
    save_ad_raw(raw)
    return True


def load_ad_daily():
    """Сводка рекламы по дням из файла → {дата: {nmId: расход ₽}}."""
    try:
        with open(AD_DAILY_PATH, encoding="utf-8") as file:
            return json.load(file).get("по дням") or {}
    except Exception as exc:
        log(f"! реклама: файл по дням не прочитан ({exc})")
        return None


def load_ad_incomplete():
    """Список окон, которые в прошлый раз добрать не удалось."""
    try:
        with open(AD_DAILY_PATH, encoding="utf-8") as file:
            return json.load(file).get("неполные окна") or []
    except Exception:
        return []


def ad_daily_from_raw(windows):
    """Сводка расхода по дням и товарам из сырых ответов fullstats.

    Расход = поле sum внутри apps[].nms[] (рубли, проверено 29.10:
    совпадает с clicks × cpc). Периодические строки без nms считаются
    по остатку дня поровну на карточки кампании (с пометкой в журнале).
    Состав кампаний меняется между прогонами (запустили/удалили) —
    тогда на одно окно приходится несколько наборов ответов, и
    простое суммирование задвоит расход. Поэтому каждое окно собираем
    от свежих записей к старым, и кампания считается ОДНИМ ответом —
    самым свежим, который её покрывает.
    """
    daily = defaultdict(lambda: defaultdict(float))
    by_window = defaultdict(list)
    for window in windows:
        key = (window.get("beginDate"), window.get("endDate"))
        by_window[key].append(window)
    special = 0
    for (begin, end), group in sorted(by_window.items()):
        covered = set()
        for window in reversed(group):        # свежие записи — в конце файла
            ids = set(window.get("ids") or [])
            if not ids or ids & covered:
                continue
            covered |= ids
            special += _window_spend(window, daily)
    if special:
        log(f"реклама: дней с неразложенным остатком расхода: {special} "
            f"(разложен поровну на карточки кампаний)")
    return daily


def _window_spend(window, daily):
    """Расход одного сырого окна в daily; дней с остатком поровну → число."""
    special = 0
    for camp in window.get("кампании") or []:
        campaign_nms = set()
        for day in camp.get("days") or []:
            date = (day.get("date") or "")[:10]
            day_total = day.get("sum") or 0
            app_sum = 0.0
            for app in day.get("apps") or []:
                nms_list = app.get("nms") or []
                if not nms_list:
                    continue
                app_sum += app.get("sum") or 0
                for nm in nms_list:
                    nm_id = str(nm.get("nmId") or "")
                    if nm_id and nm_id not in campaign_nms:
                        campaign_nms.add(nm_id)
                    if nm_id:
                        daily[date][nm_id] += nm.get("sum") or 0
            missing = day_total - app_sum
            if missing > 0.01 and campaign_nms:
                special += 1
                share = missing / len(campaign_nms)
                for nm_id in campaign_nms:
                    daily[date][nm_id] += share
    return special


def ids_active_in_period(campaigns, earliest, today):
    """ID кампаний, живших в периоде earliest…today (по меткам WB)."""
    live_ids = []
    for campaign in campaigns:
        stamps = campaign.get("timestamps") or {}
        started = (stamps.get("started") or "")[:10]
        deleted = (stamps.get("deleted") or "")[:10]
        if not started or started > today:
            continue
        # «2100-01-01» — признак живой кампании (не удалённой)
        if deleted and deleted != "2100-01-01" and deleted < earliest:
            continue
        live_ids.append(campaign.get("id"))
    return live_ids


def fetch_ads(token):
    """Расходы на рекламу Спрутс в data-api («реклама-выгрузка.json»).

    Ключ тот же (проверено 29.10.2026); fullstats по окнам ≤31 день,
    пачки дробятся до одиночек (multi-id WB отдаёт null). Сырой ответ
    пишется после каждого запроса — прерванный прогон продолжается.
    Итог по дням → «реклама-по-дням.json». Свежий ПОЛНЫЙ файл (младше
    AD_TTL_HOURS) обрабатывается без запросов к WB; неполный (есть
    окна, которые WB не отдал) живёт только AD_TTL_INCOMPLETE_HOURS —
    ближайший прогон его добирает.
    Возвращает {дата: {nmIdString: расход ₽}} или None (не вышло).
    """
    fresh = load_ad_daily()
    if fresh is not None and os.path.isfile(AD_DAILY_PATH):
        age_hours = (time.time() - os.path.getmtime(AD_DAILY_PATH)) / 3600
        ttl = (AD_TTL_HOURS if not load_ad_incomplete()
               else AD_TTL_INCOMPLETE_HOURS)
        if age_hours < ttl:
            log(f"реклама свежая ({age_hours:.1f} ч) — без запросов")
            return fresh
    earliest = earliest_sale_day()
    if not earliest:
        log("! реклама: локальных продаж нет — период не определён")
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    answer = api_get(ENDPOINT_ADVERTS, {"limit": 200}, token)
    if answer is None:
        log("! реклама: список кампаний не получен")
        return None
    campaigns = (answer.get("adverts") if isinstance(answer, dict)
                 else answer) or []
    live_ids = ids_active_in_period(campaigns, earliest, today)
    if not live_ids:
        log("реклама: активных в периоде кампаний нет — расход пустой")
        return {}
    log(f"реклама: живых кампаний {len(live_ids)}, окна {earliest}…{today}")
    raw = load_ad_raw()
    incomplete = []
    for begin, end in ad_windows(earliest, today):
        for i in range(0, len(live_ids), FULLSTATS_MAX_IDS):
            piece = live_ids[i:i + FULLSTATS_MAX_IDS]
            if not ad_group_stats(token, piece, begin, end, raw):
                incomplete.append(f"{begin}…{end}")
    if incomplete:
        log(f"! реклама: неполные окна {len(incomplete)} — "
            f"дозагрузка в следующий прогон (файл живёт "
            f"{AD_TTL_INCOMPLETE_HOURS:.0f} ч)")
    daily = ad_daily_from_raw(raw)
    with open(AD_DAILY_PATH, "w", encoding="utf-8") as file:
        json.dump({"срез": datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "период": f"{earliest}…{today}",
                   "неполные окна": incomplete,
                   "по дням": daily}, file, ensure_ascii=False, indent=1)
    total_sum = sum(sum(per_nm.values()) for per_nm in daily.values())
    log(f"реклама: окон {len(raw)}, расход за период {total_sum:.2f} ₽ "
        f"({'полная' if not incomplete else 'НЕполная'})")
    print(f"Реклама получена: окон {len(raw)}, расход {total_sum:.0f} ₽ "
          f"за {earliest}…{today}"
          + (f"; НЕПОЛНО ({len(incomplete)} окон)" if incomplete else ""))
    return daily


def fetch_cards(token):
    """Предметы карточек (nmId → предмет) → data-api/карточки.json.

    Дашборд строит из этого группы для иерархии «тип → товар»:
    колонки «тип» в выгрузке продаж нет. Свежий файл (младше недели)
    обрабатывается без запроса к WB. Возвращает словарь или None.
    """
    cards_path = os.path.join(DATA_DIR, "карточки.json")
    if os.path.isfile(cards_path):
        age_days = (time.time() - os.path.getmtime(cards_path)) / 86400
        if age_days < CARDS_TTL_DAYS:
            return None                       # свежий кэш — WB не дёргаем
    subjects = {}
    nxt = None
    for _ in range(50):                       # страниц не больше
        cursor = {"limit": 100}
        if nxt:
            cursor["next"] = nxt
        settings = {"cursor": cursor}
        if not nxt:
            settings["filter"] = {"deleted": 0, "withPhoto": 0}
        answer = api_post(ENDPOINT_CARDS, {"settings": settings}, token)
        if answer is None:
            log("! карточки: попытки исчерпаны — предметы не обновлены")
            return None
        block = answer.get("data") or {}
        for card in block.get("cards") or []:
            if card.get("subjectName"):
                subjects[str(card.get("nmID"))] = card["subjectName"]
        nxt = (block.get("cursor") or {}).get("next")
        if not nxt:
            break
    with open(cards_path, "w", encoding="utf-8") as file:
        json.dump({"срез": datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "предметы": subjects}, file, ensure_ascii=False, indent=1)
    log(f"карточки: предметов {len(subjects)} → карточки.json")
    return subjects


def fetch_delta(token, state):
    """Продажи и заказы с метки lastChangeDate.

    Возвращает (added_sales, added_orders, orders_ok). Если заказы
    получлись пустыми (сеть/лимит) — метку НЕ сдвигаем и «сегодня уже
    выгружалось» не включаем: следующий прогон доберёт и заказы, и
    продажи заново (дедупликация не даст дублей). Сдвинуть метку при
    потерянных заказах — навсегда пропустить их в выгрузку.
    """
    params = {"dateFrom": state["last_change"]}
    sales_rows = api_get(ENDPOINT_SALES, params, token)
    if sales_rows is None:
        log("! продажи: попытки исчерпаны — выгрузка не состоялась")
        return None
    # заказы нужны для «Заказы шт» и выкупа; их метка своя, берём с запасом
    orders_rows = api_get(ENDPOINT_ORDERS, params, token)
    if orders_rows is None:
        log("! заказы не получены — метка не сдвинута, доберутся "
            "в следующий прогон")
        return 0, 0, False

    seen = read_existing_keys()
    added_sales = write_jsonl(sales_rows, seen)
    added_orders = write_jsonl(orders_rows, seen)
    new_max = state["last_change"]
    for row in sales_rows + orders_rows:
        change = row.get("lastChangeDate")
        if change and change > new_max:
            new_max = change
    state["last_change"] = new_max
    state["last_ok_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    log(f"продажи +{added_sales}, заказы +{added_orders} "
        f"(метка до {new_max})")
    return added_sales, added_orders, True


def main():
    """Проверка метки дня → выгрузка → сводка CSV → остатки.

    Флаг --регенерация: пересобрать только CSV из локальных данных
    (без API) — после обновления карты артикулов.
    Флаг --реклама: только догрузить рекламу Спрутс и пересобрать CSV
    (продажи не трогаем; работает даже если сегодня уже выгружалось).
    Флаг --история: разовая дозагрузка старых продаж — метку lastChange
    откатить к 2026-06-01 (выгрузка, начатая с 01.09, пропустила
    июль-август: их продажи уже не менялись после 01.09). Дедупликация
    по srid не даст записать уже имеющиеся строки заново.
    """
    if "--регенерация" in sys.argv:
        os.makedirs(DATA_DIR, exist_ok=True)
        regenerate()
        return
    if "--реклама" in sys.argv:
        token = load_token()
        if not token:
            log("! ключ WB_API_KEY не найден в .env")
            print("Не найден ключ WB_API_KEY в .env", file=sys.stderr)
            sys.exit(1)
        ad_daily = fetch_ads(token)
        if ad_daily is not None:
            rows = read_jsonl_rows()
            csv_rows, pairs = aggregate_csv_rows(rows, ad_daily)
            write_csv(csv_rows, load_map())
            print(f"CSV пересобран с рекламой: пар день×артикул {pairs}")
        return
    state = load_state()
    if "--история" in sys.argv:
        state["last_change"] = "2026-06-01T00:00:00"
        state["last_ok_date"] = ""
        log("дозагрузка истории: метка откатана к 2026-06-01")
        print("Дозагрузка истории (с 2026-06-01) — может идти минуты")
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_ok_date", "").startswith(today):
        print("Сегодня уже выгружалось — тихий выход "
              f"(последний прогон {state['last_ok_date']})")
        return

    token = load_token()
    if not token:
        log("! ключ WB_API_KEY не найден в .env")
        print("Не найден ключ WB_API_KEY в .env рядом с проектом",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(DATA_DIR, exist_ok=True)
    started = datetime.now()
    log("прогон начат")
    mark_old_rows()

    added_sales, added_orders, orders_ok = fetch_delta(token, state)
    if added_sales is None:
        log("! выгрузка не состоялась (API не ответил после попыток)")
        print("Выгрузка не удалась — детали в журнал-выгрузки.txt",
              file=sys.stderr)
        sys.exit(2)

    rows = read_jsonl_rows()
    stock_ok = fetch_stocks(token)
    reports_n = fetch_reports(token)
    cards_subj = fetch_cards(token)
    ad_daily = fetch_ads(token)

    csv_rows, pairs = aggregate_csv_rows(rows, ad_daily)
    write_csv(csv_rows, load_map())
    log(f"готово: строк {len(rows)}, сводка пар день×артикул {pairs}, "
        f"остатки {'ок' if stock_ok is not None else 'НЕ ок'}, "
        f"отчёты {'ок' if reports_n is not None else 'НЕ ок'}, "
        f"карточки {'ок' if cards_subj is not None else 'кэш/НЕ ок'}, "
        f"реклама {'ок' if ad_daily is not None else 'НЕ ок'} "
        f"за {int((datetime.now() - started).total_seconds())} с")

    print(f"Выгрузка готова: {len(rows)} строк продаж/заказов, "
          f"пары день×артикул {pairs}; CSV: {os.path.basename(CSV_PATH)}; "
          f"остатки: {'ок' if stock_ok is not None else 'не получены'}; "
          f"отчёты реализации: "
          f"{'ок' if reports_n is not None else 'не получены'}; "
          f"карточки: "
          f"{'ок' if cards_subj is not None else 'кэш свежий/нет'}; "
          f"реклама: {'ок' if ad_daily is not None else 'не получена'}")
    if not orders_ok:
        log("! заказы в этом прогоне не получены — метка не сдвинута")
        print("ВНИМАНИЕ: заказы не получены — следующий прогон доберёт их "
              "заново (метка не сдвинута)", file=sys.stderr)


if __name__ == "__main__":
    main()
