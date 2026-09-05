"""Тесты хранилища: удержание размера, сводка о живости, выбор аналогов.

Проверяются три решения, найденные на живых данных: файл базы не должен
навсегда сохранять размер своего пика, сводка о живости не должна требовать
нового обхода, и позиция не должна попадать в число собственных «аналогов».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gpudeals.models import ItemKind, Offer
from gpudeals.storage import (
    class_prices,
    compact,
    connect,
    prune_old_observations,
    record_crawl,
    save_observations,
    shop_summary,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "prices.sqlite3"


def card(identity_source: str, price: int) -> Offer:
    return Offer(
        shop="technodom",
        kind=ItemKind.CARD,
        title=f"Видеокарта {identity_source}",
        price=price,
        url="https://example.kz",
        class_key="rtx5070-12",
        part_number=identity_source,
        chip="rtx5070",
        memory_gb=12,
    )


def insert_at(conn, identity: str, price: int, days_ago: float) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO observations (observed_at, shop, kind, identity, title,
               price, url, class_key, chip, memory_gb, in_stock)
           VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
        (stamp, "technodom", ItemKind.CARD.value, identity, "тест", price,
         "https://example.kz", "rtx5070-12", "rtx5070", 12),
    )


def test_class_prices_excludes_the_offer_itself(db_path) -> None:
    """Прошлая, более высокая цена самой позиции — не «аналог».

    Иначе упавшая карта сравнивается со собственной прежней ценой, и падение
    выглядит выгоднее, чем оно есть.
    """
    with connect(db_path) as conn:
        insert_at(conn, "technodom:pn:self", 500_000, days_ago=2)
        insert_at(conn, "technodom:pn:peer-1", 460_000, days_ago=1)
        insert_at(conn, "technodom:pn:peer-2", 470_000, days_ago=1)

        with_self = class_prices(conn, ItemKind.CARD, "rtx5070-12")
        without_self = class_prices(
            conn, ItemKind.CARD, "rtx5070-12", exclude_identity="technodom:pn:self"
        )

    assert sorted(with_self) == [460_000, 470_000, 500_000]
    assert sorted(without_self) == [460_000, 470_000]


def test_shop_summary_reads_last_crawl_per_shop(db_path) -> None:
    with connect(db_path) as conn:
        record_crawl(conn, "technodom", 81, True)
        record_crawl(conn, "kaspi", 49, True)
        record_crawl(conn, "dns", 0, False, "timeout")

    with connect(db_path) as conn:
        summary = shop_summary(conn, ["technodom", "kaspi", "dns"])

    assert summary == [("technodom", 81, True), ("kaspi", 49, True), ("dns", 0, False)]


def test_shop_summary_reports_never_crawled_shop_as_failure(db_path) -> None:
    """Магазин без единой записи — это и есть поломка, о которой сводка молчала бы."""
    with connect(db_path) as conn:
        record_crawl(conn, "technodom", 81, True)

    with connect(db_path) as conn:
        assert shop_summary(conn, ["technodom", "sulpak"]) == [
            ("technodom", 81, True),
            ("sulpak", 0, False),
        ]


def test_compact_shrinks_file_after_retention(db_path) -> None:
    """auto_vacuum выключен: без VACUUM файл навсегда сохраняет размер пика.

    Он коммитится в репозиторий после каждого обхода, поэтому пик потом
    пересохраняется по одиннадцать раз в сутки.
    """
    with connect(db_path) as conn:
        # Достаточно строк, чтобы файл заметно вырос за пределы одной страницы.
        save_observations(conn, [card(f"GV-N5070-{i}", 400_000 + i) for i in range(4_000)])

    peak = db_path.stat().st_size

    with connect(db_path) as conn:
        for i in range(4_000):
            insert_at(conn, f"technodom:pn:gv-n5070-{i}", 400_000 + i, days_ago=45)
        removed = prune_old_observations(conn, days=30)

    assert removed == 4_000
    # До сжатия удаление места не освобождает.
    assert db_path.stat().st_size >= peak

    freed = compact(db_path)
    assert freed > 0
    assert db_path.stat().st_size < peak


def test_compact_on_missing_file_is_noop(tmp_path) -> None:
    assert compact(tmp_path / "нет-такого.sqlite3") == 0
