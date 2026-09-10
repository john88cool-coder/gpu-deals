"""Хранилище: SQLite одним файлом, без сервера.

Схема с первого дня знает про тип товара (карта/сборка) и общий GPU-ключ,
чтобы добавление сборок не потребовало переписывать историю цен.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import DB_PATH
from .models import ItemKind, Offer

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY,
    observed_at   TEXT    NOT NULL,
    shop          TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    identity      TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    price         INTEGER NOT NULL,
    url           TEXT    NOT NULL,
    class_key     TEXT,
    part_number   TEXT,
    chip          TEXT,
    memory_gb     INTEGER,
    brand         TEXT,
    shop_old_price     INTEGER,
    shop_discount_pct  INTEGER,
    in_stock      INTEGER NOT NULL DEFAULT 1,
    stock_note    TEXT,
    sku           TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_identity ON observations(identity, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_class    ON observations(kind, class_key, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_time     ON observations(observed_at);

-- Уже отправленные уведомления: повтор по позиции только при новом снижении.
CREATE TABLE IF NOT EXISTS alerts (
    identity     TEXT PRIMARY KEY,
    alerted_at   TEXT    NOT NULL,
    alerted_price INTEGER NOT NULL,
    delivered_at TEXT
);

-- Итоги обходов: нужны, чтобы заметить молча сломавшийся парсер.
CREATE TABLE IF NOT EXISTS crawls (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    shop        TEXT NOT NULL,
    item_count  INTEGER NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_crawls_shop ON crawls(shop, started_at);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    """Разовые миграции существующих баз.

    delivered_at добавляется вместе с обратным заполнением: алерты, сделанные
    до появления колонки, реально уходили в Telegram — помечаем доставленными,
    иначе первый запуск после обновления переотправил бы всю старую историю.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    if "delivered_at" not in columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN delivered_at TEXT")
        conn.execute("UPDATE alerts SET delivered_at = alerted_at WHERE delivered_at IS NULL")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_observations(conn: sqlite3.Connection, offers: Iterable[Offer]) -> int:
    stamp = now_iso()
    rows = [
        (
            stamp, o.shop, o.kind.value, o.identity, o.title, o.price, o.url,
            o.class_key, o.part_number, o.chip, o.memory_gb, o.brand,
            o.shop_old_price, o.shop_discount_pct, int(o.in_stock), o.stock_note, o.sku,
        )
        for o in offers
    ]
    conn.executemany(
        """INSERT INTO observations (
               observed_at, shop, kind, identity, title, price, url,
               class_key, part_number, chip, memory_gb, brand,
               shop_old_price, shop_discount_pct, in_stock, stock_note, sku)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def price_history(
    conn: sqlite3.Connection, identity: str, window_days: int
) -> list[tuple[str, int]]:
    """Наблюдения по позиции за окно тренда, от старых к новым.

    Только доступные наблюдения: цена позиции «под заказ» за 800 000 ₸ не
    должна создавать ложное «упало на 50%» при возврате за прежние 400 000 ₸.
    Возвращение в продажу — отдельное событие (Signal.RESTOCK).
    """
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    cur = conn.execute(
        """SELECT observed_at, price FROM observations
           WHERE identity = ? AND observed_at >= ? AND in_stock = 1
           ORDER BY observed_at""",
        (identity, since),
    )
    return [(r["observed_at"], r["price"]) for r in cur]


def class_prices(
    conn: sqlite3.Connection,
    kind: ItemKind,
    class_key: str,
    window_days: int = 3,
    exclude_identity: str | None = None,
) -> list[int]:
    """Свежие цены по классу для медианы. Раздельно по типу товара.

    Только позиции в наличии: цена товара «под заказ» — не рыночная цена,
    и она занижала бы медиану. `exclude_identity` убирает из выборки саму
    оцениваемую позицию: её прошлые наблюдения — не «аналоги». Иначе упавшая
    в цене карта сравнивалась бы со своей же прежней ценой, и падение
    выглядело бы выгоднее, чем оно есть.
    """
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    cur = conn.execute(
        """SELECT identity, MIN(price) AS price FROM observations
           WHERE kind = ? AND class_key = ? AND observed_at >= ? AND in_stock = 1
                 AND (? IS NULL OR identity <> ?)
           GROUP BY identity""",
        (kind.value, class_key, since, exclude_identity, exclude_identity),
    )
    return [r["price"] for r in cur]


def class_floor_for_cards(
    conn: sqlite3.Connection, window_days: int = 3
) -> dict[str, int]:
    """Минимальная цена отдельной карты по каждому классу, по всем магазинам.

    Только в наличии: остаток за платформу считается от цены, за которую карту
    реально можно взять сейчас. База для остатка за платформу у сборок:
    сравнивать надо с рыночным минимумом, а не с ценой того же магазина, иначе
    остаток завышается.
    """
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    cur = conn.execute(
        """SELECT class_key, MIN(price) AS floor FROM observations
           WHERE kind = ? AND class_key IS NOT NULL AND observed_at >= ?
                 AND in_stock = 1
           GROUP BY class_key""",
        (ItemKind.CARD.value, since),
    )
    return {row["class_key"]: row["floor"] for row in cur}


def class_best_offers(
    conn, chips: frozenset[str], skip_memory_gb: int, window_days: int = 3
) -> dict[str, tuple[str, int]]:
    """Лучшее предложение по каждому классу: (магазин, цена).

    Только в наличии, только интересующие чипы; MIN(price) с «голыми»
    колонками отдаёт shop из строки минимума — документированное поведение
    SQLite.
    """
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    chips_q = ",".join("?" * len(chips))
    cur = conn.execute(
        f"""SELECT class_key, MIN(price) AS price, shop
            FROM observations
            WHERE kind = 'card' AND class_key IS NOT NULL AND in_stock = 1
                  AND chip IN ({chips_q})
                  AND (memory_gb IS NULL OR memory_gb > ?)
                  AND observed_at >= ?
            GROUP BY class_key""",
        (*sorted(chips), skip_memory_gb, since),
    )
    return {row["class_key"]: (row["shop"], row["price"]) for row in cur}


def class_best_deals(
    conn, chips: frozenset[str], skip_memory_gb: int, window_days: int = 2
) -> list[tuple[str, int, str, str, str]]:
    """Самая выгодная позиция каждого класса для свода: (class_key, цена,
    магазин, заголовок, ссылка).

    Выгодная = минимальная СРЕДИ ПОСЛЕДНИХ цен позиций, а не минимум за окно:
    минимум мог быть три дня назад по позиции, которой уже нет или которая
    подорожала. Последнее наблюдение каждой позиции выбирается БЕЗ фильтра
    наличия (иначе вчерашняя цена отсутствующего сегодня товара выигрывает у
    доступного конкурента), и только затем позиция проверяется на in_stock.
    """
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    chips_q = ",".join("?" * len(chips))
    cur = conn.execute(
        f"""SELECT class_key, price, shop, title, url FROM (
              SELECT identity, class_key, price, shop, title, url, in_stock,
                     MAX(observed_at) AS at, MAX(id) AS max_id
              FROM observations
              WHERE kind = 'card' AND class_key IS NOT NULL
                    AND chip IN ({chips_q})
                    AND (memory_gb IS NULL OR memory_gb > ?)
                    AND observed_at >= ?
              GROUP BY identity
            )
            WHERE in_stock = 1
            GROUP BY class_key
            HAVING price = MIN(price)
            ORDER BY class_key""",
        (*sorted(chips), skip_memory_gb, since),
    )
    return [
        (row["class_key"], row["price"], row["shop"], row["title"], row["url"])
        for row in cur
    ]


def last_price(conn: sqlite3.Connection, identity: str) -> int | None:
    """Последняя наблюдённая цена позиции (любое наличие) или None."""
    row = conn.execute(
        """SELECT price FROM observations WHERE identity = ?
           ORDER BY observed_at DESC, id DESC LIMIT 1""",
        (identity,),
    ).fetchone()
    return row["price"] if row else None


def last_in_stock(conn: sqlite3.Connection, identity: str) -> bool | None:
    """Была ли позиция в наличии при последней записи. None — записей нет."""
    row = conn.execute(
        """SELECT in_stock FROM observations WHERE identity = ?
           ORDER BY observed_at DESC, id DESC LIMIT 1""",
        (identity,),
    ).fetchone()
    return None if row is None else bool(row["in_stock"])


def best_build_residual(
    conn: sqlite3.Connection,
    floors: dict[str, int],
    budget: int,
    chips: frozenset[str] | None = None,
    skip_memory_gb: int | None = None,
    window_days: int = 3,
) -> tuple[str, int, str, int] | None:
    """Сборка с наименьшим остатком за платформу: (класс, цена, магазин, остаток).

    Остаток = цена сборки минус минимум за такую же карту отдельным товаром
    (`floors` из `class_floor_for_cards`). Сборки без карты в базе остаток
    не имеют и не участвуют. Только в наличии и не дороже бюджета. Если
    переданы `chips` и `skip_memory_gb`, применяются и фильтры интересов
    владельца: сборка со старой картой не должна становиться «лучшей».
    """
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    interest = ""
    params: list = [budget]
    if chips is not None:
        chips_q = ",".join("?" * len(chips))
        interest = f" AND chip IN ({chips_q}) AND (memory_gb IS NULL OR memory_gb > ?)"
        params.extend(sorted(chips))
        params.append(skip_memory_gb)
    params.append(since)
    best: tuple[str, int, str, int] | None = None
    for row in conn.execute(
        f"""SELECT class_key, MIN(price) AS price, shop
            FROM observations
            WHERE kind = 'build' AND in_stock = 1 AND price <= ?{interest}
                  AND observed_at >= ?
            GROUP BY class_key""",
        params,
    ):
        floor = floors.get(row["class_key"])
        if not floor:
            continue
        residual = row["price"] - floor
        if best is None or residual < best[3]:
            best = (row["class_key"], row["price"], row["shop"], residual)
    return best


def last_alert(conn: sqlite3.Connection, identity: str) -> int | None:
    """Цена последнего ДОСТАВЛЕННОГО алерта по позиции.

    Находки, помеченные, но не отправленные (сбой Telegram), дедупликацию не
    расходуют: следующий обход при той же цене отправит их.
    """
    cur = conn.execute(
        """SELECT alerted_price FROM alerts WHERE identity = ?
                 AND delivered_at IS NOT NULL""",
        (identity,),
    )
    row = cur.fetchone()
    return row["alerted_price"] if row else None


def mark_alerts_delivered(conn: sqlite3.Connection, identities: list[str]) -> None:
    """Подтверждает доставку алертов после успешной отправки в Telegram."""
    conn.executemany(
        """UPDATE alerts SET delivered_at = ? WHERE identity = ? AND delivered_at IS NULL""",
        [(now_iso(), identity) for identity in identities],
    )


def record_alert(conn: sqlite3.Connection, identity: str, price: int) -> None:
    """Фиксирует НАЙДЕННУЮ находку (не отправленную): delivered_at остаётся
    NULL до подтверждения доставки — см. mark_alerts_delivered."""
    conn.execute(
        """INSERT INTO alerts (identity, alerted_at, alerted_price, delivered_at)
           VALUES (?,?,?,NULL)
           ON CONFLICT(identity) DO UPDATE SET alerted_at=excluded.alerted_at,
                                               alerted_price=excluded.alerted_price,
                                               delivered_at=NULL""",
        (identity, now_iso(), price),
    )


def record_crawl(
    conn: sqlite3.Connection, shop: str, item_count: int, ok: bool, error: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO crawls (started_at, shop, item_count, ok, error) VALUES (?,?,?,?,?)",
        (now_iso(), shop, item_count, int(ok), error),
    )


def prune_old_observations(conn: sqlite3.Connection, days: int = 30) -> int:
    """Удаляет наблюдения старше окна.

    Тренд считается за 14 дней, медианы за 3, поэтому глубже 30 дней данные
    не нужны ни одному сигналу. База коммитится в репозиторий после каждого
    обхода — без удержания она росла бы на гигабайты в год.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM observations WHERE observed_at < ?", (since,))
    conn.execute(
        """DELETE FROM alerts WHERE identity NOT IN (SELECT DISTINCT identity FROM observations)"""
    )
    return cur.rowcount


def compact(path: Path | None = None) -> int:
    """Сжимает файл базы, возвращает освободившиеся байты.

    `auto_vacuum` в базе выключен, поэтому удалённые retention-ом страницы
    остаются в файле: без VACUUM он навсегда сохраняет размер своего пика. Файл
    коммитится в репозиторий после каждого обхода, а значит каждый пик потом
    пересохраняется 11 раз в сутки.

    Отдельной функцией, а не внутри `prune_old_observations`: VACUUM нельзя
    выполнить в транзакции, а `connect()` держит открытую транзакцию до commit.
    """
    target = path or DB_PATH
    if not target.exists():
        return 0
    before = target.stat().st_size
    conn = sqlite3.connect(target, isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    return max(before - target.stat().st_size, 0)


def previous_item_count(conn: sqlite3.Connection, shop: str) -> int | None:
    """Сколько позиций дал магазин в прошлый успешный обход."""
    cur = conn.execute(
        """SELECT item_count FROM crawls WHERE shop = ? AND ok = 1 AND item_count > 0
           ORDER BY started_at DESC LIMIT 1""",
        (shop,),
    )
    row = cur.fetchone()
    return row["item_count"] if row else None


def last_successful_crawl(conn: sqlite3.Connection, shop: str) -> str | None:
    """Когда последний раз магазин успешно обошли (ISO-время или None)."""
    row = conn.execute(
        """SELECT started_at FROM crawls WHERE shop = ? AND ok = 1
           ORDER BY started_at DESC LIMIT 1""",
        (shop,),
    ).fetchone()
    return row["started_at"] if row else None


def shop_summary(conn: sqlite3.Connection, shops: Iterable[str]) -> list[tuple[str, int, bool]]:
    """Итог последнего обхода по каждому магазину: (магазин, позиций, успех).

    Читается из таблицы `crawls`, а не новым обходом: сводка о живости не должна
    сама быть третьим полным обходом всех каталогов вместе с браузерным DNS.
    Магазин, по которому записей нет вовсе, попадает в сводку как ошибка — это и
    есть та поломка, о которой сводка должна сообщать.
    """
    summary: list[tuple[str, int, bool]] = []
    for shop in shops:
        row = conn.execute(
            """SELECT item_count, ok FROM crawls WHERE shop = ?
               ORDER BY started_at DESC LIMIT 1""",
            (shop,),
        ).fetchone()
        if row is None:
            summary.append((shop, 0, False))
        else:
            summary.append((shop, row["item_count"], bool(row["ok"])))
    return summary
