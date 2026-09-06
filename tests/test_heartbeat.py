"""Тесты сводки о живости.

Раньше `heartbeat` делал полный обход всех семи магазинов вместе с браузерным
DNS и отправлял найденные позиции, помечая их как отправленные. Workflow
heartbeat базу не коммитит, поэтому пометка терялась и та же находка приходила
повторно со следующим обходом.
"""

from __future__ import annotations

import pytest

from gpudeals import crawler
from gpudeals.storage import connect, record_crawl


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Изолированная база: `connect()` без пути берёт DB_PATH из конфига."""
    path = tmp_path / "prices.sqlite3"
    monkeypatch.setattr("gpudeals.storage.DB_PATH", path)
    return path


def test_heartbeat_reads_database_without_crawling(db, monkeypatch) -> None:
    def fail_if_crawled(*args, **kwargs):  # pragma: no cover — вызов = провал теста
        raise AssertionError("heartbeat не должен запускать обход")

    monkeypatch.setattr(crawler.asyncio, "run", fail_if_crawled)

    with connect(db) as conn:
        record_crawl(conn, "technodom", 81, True)
        record_crawl(conn, "kaspi", 49, True)

    notifier = RecordingNotifier()
    crawler.send_heartbeat(notifier, shops=["technodom", "kaspi"])

    assert len(notifier.sent) == 1
    assert "2/2 магазинов опрошено, 130 позиций" in notifier.sent[0]


def test_heartbeat_surfaces_shop_with_no_records(db) -> None:
    with connect(db) as conn:
        record_crawl(conn, "technodom", 81, True)

    notifier = RecordingNotifier()
    crawler.send_heartbeat(notifier, shops=["technodom", "dns"])

    assert "1/2" in notifier.sent[0]
    assert "dns: ошибка" in notifier.sent[0]


def test_heartbeat_covers_all_shops_by_default(db) -> None:
    notifier = RecordingNotifier()
    crawler.send_heartbeat(notifier)

    # Пустая база: все семь магазинов попадают в сводку как неопрошенные.
    assert "0/7 магазинов опрошено" in notifier.sent[0]


def _insert_card(conn, identity: str, price: int, shop: str,
                 class_key: str = "rtx5070-12", chip: str = "rtx5070") -> None:
    conn.execute(
        """INSERT INTO observations (observed_at, shop, kind, identity, title,
               price, url, class_key, chip, memory_gb, in_stock)
           VALUES (datetime('now'), ?, 'card', ?, 'тест', ?, 'https://e.kz',
                   ?, ?, 12, 1)""",
        (shop, identity, price, class_key, chip),
    )


def test_heartbeat_includes_buyers_guide_with_target_status(db) -> None:
    """Шпаргалка: лучшая цена по каждому классу и сколько до цели владельца."""
    with connect(db) as conn:
        _insert_card(conn, "d1", 363_990, "dns")
        _insert_card(conn, "s1", 370_000, "sulpak")
        _insert_card(conn, "t1", 530_000, "technodom",
                     class_key="rtx5070ti-16", chip="rtx5070ti")

    notifier = RecordingNotifier()
    crawler.send_heartbeat(notifier, shops=["technodom"])

    text = notifier.sent[0]
    assert "Шпаргалка покупателя" in text
    assert "rtx5070-12: 363 990 ₸ (dns)" in text, "лучший оффер класса, не все"
    assert "цель 365 000 ₸ ✓" in text, "363 990 ≤ 365 000 — цель достигнута"
    assert "rtx5070ti-16: 530 000 ₸ (technodom) — до цели 5 000 ₸" in text


def test_heartbeat_without_offers_skips_guide(db) -> None:
    with connect(db) as conn:
        record_crawl(conn, "technodom", 81, True)

    notifier = RecordingNotifier()
    crawler.send_heartbeat(notifier, shops=["technodom"])

    assert len(notifier.sent) == 1
    assert "Шпаргалка" not in notifier.sent[0]


def test_heartbeat_includes_best_build_by_residual(db) -> None:
    """Шпаргалка показывает и лучшую сборку: цена минус минимум карты."""
    with connect(db) as conn:
        _insert_card(conn, "card", 400_000, "technodom")
        conn.execute(
            """INSERT INTO observations (observed_at, shop, kind, identity, title,
                   price, url, class_key, chip, memory_gb, in_stock)
               VALUES (datetime('now'), 'technodom', 'build', 'b1',
                       'Компьютер RTX 5070', 870000, 'https://e.kz',
                       'rtx5070-12', 'rtx5070', 12, 1)"""
        )
        conn.execute(
            """INSERT INTO observations (observed_at, shop, kind, identity, title,
                   price, url, class_key, chip, memory_gb, in_stock)
               VALUES (datetime('now'), 'technodom', 'build', 'b2',
                       'Компьютер RTX 5070 дорогой', 990000, 'https://e.kz',
                       'rtx5070-12', 'rtx5070', 12, 1)"""
        )

    notifier = RecordingNotifier()
    crawler.send_heartbeat(notifier, shops=["technodom"])

    text = notifier.sent[0]
    assert "Сборка rtx5070-12: 870 000 ₸ (technodom) — остаток за платформу 470 000 ₸" in text
    # Дорогая сборка того же класса в шпаргалку не попала.
    assert "990 000" not in text
