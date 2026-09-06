"""Фильтр по объёму видеопамяти и покрытие DNS.

Решение владельца: карта покупается с памятью больше 8 ГБ, позиции с меньшим
объёмом не рассматриваются вовсе — не пишутся в базу и не порождают
уведомлений. RTX 3080 на 10 ГБ проходит, поэтому правило именно «больше 8»,
а не «от 12».

DNS: пагинация каталога не работает (проверено вживую — запрос ?page=2 отдаёт
содержимое первой страницы), поэтому покрытие добирается страницами-подборками.
"""

from __future__ import annotations

import asyncio

import pytest

from gpudeals import crawler
from gpudeals.models import ItemKind, Offer
from gpudeals.shops import dns
from gpudeals.storage import connect


def offer(memory_gb: int | None, kind: ItemKind = ItemKind.CARD) -> Offer:
    return Offer(
        shop="fake",
        kind=kind,
        title=f"Видеокарта {memory_gb or '?'} ГБ",
        price=100_000,
        url="https://example.kz",
        chip="rtx5070" if kind is ItemKind.CARD else None,
        memory_gb=memory_gb,
        class_key=f"rtx5070-{memory_gb}" if memory_gb else None,
        part_number=f"GV-{memory_gb}",
    )


class FakeShop:
    SHOP = "fake"

    @staticmethod
    async def fetch(client):  # noqa: ANN001 — интерфейс парсера
        return [
            offer(4),
            offer(8),
            offer(8, kind=ItemKind.BUILD),
            offer(None),
            offer(10),
            offer(16),
            offer(16, kind=ItemKind.BUILD),
        ]


def test_crawl_drops_chips_outside_owner_interest(tmp_path, monkeypatch) -> None:
    """Интересуют только новейшие серии: RTX 5080 и 30-я серия не собираются,
    даже если памяти больше 8 ГБ. Неопознанный чип — тоже мимо."""

    class FakeShop:
        SHOP = "fake"

        @staticmethod
        async def fetch(client):  # noqa: ANN001
            return [
                offer(16),                                     # rtx5070 — остаётся
                Offer(
                    shop="fake", kind=ItemKind.CARD,
                    title="Видеокарта RTX 5080 16GB", price=100_000,
                    url="https://example.kz", chip="rtx5080", memory_gb=16,
                    class_key="rtx5080-16", part_number="GV-5080",
                ),
                Offer(
                    shop="fake", kind=ItemKind.CARD,
                    title="Видеокарта RTX 3060 12GB", price=100_000,
                    url="https://example.kz", chip="rtx3060", memory_gb=12,
                    class_key="rtx3060-12", part_number="GV-3060",
                ),
                Offer(
                    shop="fake", kind=ItemKind.CARD,
                    title="Видеокарта неведомая 16GB", price=100_000,
                    url="https://example.kz", chip=None, memory_gb=16,
                    class_key=None, part_number="GV-X",
                ),
            ]

    monkeypatch.setitem(crawler.REGISTRY, "fake", FakeShop)
    db = tmp_path / "db.sqlite3"
    monkeypatch.setattr("gpudeals.storage.DB_PATH", db)

    asyncio.run(crawler.crawl(["fake"]))

    with connect(db) as conn:
        saved = {r["identity"] for r in conn.execute("SELECT identity FROM observations")}
    assert saved == {"fake:pn:gv-16"}


@pytest.fixture
def isolated_crawl(tmp_path, monkeypatch):
    """Фейковый магазин и база во временной папке: сети и живой базы нет."""
    monkeypatch.setitem(crawler.REGISTRY, "fake", FakeShop)
    monkeypatch.setattr("gpudeals.storage.DB_PATH", tmp_path / "prices.sqlite3")
    return tmp_path / "prices.sqlite3"


def test_crawl_skips_positions_at_or_below_vram_floor(isolated_crawl) -> None:
    findings, _, _ = asyncio.run(crawler.crawl(["fake"]))

    with connect(isolated_crawl) as conn:
        saved = {row["identity"] for row in conn.execute("SELECT identity FROM observations")}

    kept = {"fake:pn:gv-none", "fake:pn:gv-10", "fake:pn:gv-16"}
    # «Неизвестно» остаётся: за ним может прятаться 16 ГБ.
    assert saved == kept


def test_crawl_records_raw_item_count(isolated_crawl) -> None:
    """Таблица crawls измеряет здоровье парсера, а не наш интерес.

    Тревога «магазин вернул 0» не должна срабатывать там, где магазин жив,
    но весь его ассортимент отфильтрован по объёму.
    """
    asyncio.run(crawler.crawl(["fake"]))

    with connect(isolated_crawl) as conn:
        row = conn.execute("SELECT item_count FROM crawls WHERE shop = 'fake'").fetchone()

    assert row["item_count"] == 7


def test_vram_floor_is_strictly_greater_than_eight() -> None:
    """RTX 3080 на 10 ГБ должен проходить: порог «больше 8», не «от 12»."""
    from gpudeals.config import Thresholds

    assert Thresholds().skip_memory_gb == 8


def test_dns_recipe_targets_cover_big_memory_models() -> None:
    urls = dns.recipe_targets()

    assert urls[0] == dns.CATALOG_URL
    slugs = [u.rstrip("/").rsplit("/", 1)[-1] for u in urls[1:]]
    assert slugs == [
        "rtx-5070",
        "rtx-5070-ti",
        "rtx-5080",
        "radeon-rx-9070",
        "radeon-rx-9070-xt",
        "radeon-rx-9060-xt-16-gb",
    ]
