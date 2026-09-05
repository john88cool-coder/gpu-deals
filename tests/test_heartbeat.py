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
