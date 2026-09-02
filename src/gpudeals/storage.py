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
    alerted_price INTEGER NOT NULL
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


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
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
    """Наблюдения по позиции за окно тренда, от старых к новым."""
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    cur = conn.execute(
        """SELECT observed_at, price FROM observations
           WHERE identity = ? AND observed_at >= ?
           ORDER BY observed_at""",
        (identity, since),
    )
    return [(r["observed_at"], r["price"]) for r in cur]


def class_prices(
    conn: sqlite3.Connection, kind: ItemKind, class_key: str, window_days: int = 3
) -> list[int]:
    """Свежие цены по классу для медианы. Раздельно по типу товара."""
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    cur = conn.execute(
        """SELECT identity, MIN(price) AS price FROM observations
           WHERE kind = ? AND class_key = ? AND observed_at >= ?
           GROUP BY identity""",
        (kind.value, class_key, since),
    )
    return [r["price"] for r in cur]


def class_floor_for_cards(
    conn: sqlite3.Connection, window_days: int = 3
) -> dict[str, int]:
    """Минимальная цена отдельной карты по каждому классу, по всем магазинам.

    База для остатка за платформу у сборок: сравнивать надо с рыночным минимумом,
    а не с ценой того же магазина, иначе остаток завышается.
    """
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    cur = conn.execute(
        """SELECT class_key, MIN(price) AS floor FROM observations
           WHERE kind = ? AND class_key IS NOT NULL AND observed_at >= ?
           GROUP BY class_key""",
        (ItemKind.CARD.value, since),
    )
    return {row["class_key"]: row["floor"] for row in cur}


def last_alert(conn: sqlite3.Connection, identity: str) -> int | None:
    cur = conn.execute("SELECT alerted_price FROM alerts WHERE identity = ?", (identity,))
    row = cur.fetchone()
    return row["alerted_price"] if row else None


def record_alert(conn: sqlite3.Connection, identity: str, price: int) -> None:
    conn.execute(
        """INSERT INTO alerts (identity, alerted_at, alerted_price) VALUES (?,?,?)
           ON CONFLICT(identity) DO UPDATE SET alerted_at=excluded.alerted_at,
                                               alerted_price=excluded.alerted_price""",
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


def previous_item_count(conn: sqlite3.Connection, shop: str) -> int | None:
    """Сколько позиций дал магазин в прошлый успешный обход."""
    cur = conn.execute(
        """SELECT item_count FROM crawls WHERE shop = ? AND ok = 1 AND item_count > 0
           ORDER BY started_at DESC LIMIT 1""",
        (shop,),
    )
    row = cur.fetchone()
    return row["item_count"] if row else None
