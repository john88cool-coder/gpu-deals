"""Тесты качества алертов: наличие, кросс-магазинное сравнение, целевые цены.

Три ответа на вопрос «что делать с этим уведомлением»: брать здесь, смотреть в
другом магазине или ждать. Отсутствующий товар не будит вообще, но история цен
его сохраняет.
"""

from __future__ import annotations

import asyncio

import pytest

from gpudeals import crawler
from gpudeals.config import Settings, Thresholds, WatchedModel
from gpudeals.evaluate import Signal, evaluate
from gpudeals.models import ItemKind, Offer
from gpudeals.report import format_offer
from gpudeals.shops import forcecom, sulpak
from gpudeals.storage import (
    class_floor_for_cards,
    class_prices,
    connect,
    record_alert,
    save_observations,
)


def offer(shop: str, price: int, **overrides) -> Offer:
    return Offer(
        shop=shop,
        kind=ItemKind.CARD,
        title=f"Видеокарта RTX 5070 {shop}",
        price=price,
        url="https://example.kz",
        class_key="rtx5070-12",
        chip="rtx5070",
        memory_gb=12,
        part_number=overrides.pop("pn", f"GV-{shop.upper()}"),
        **overrides,
    )


# --- наличие ---------------------------------------------------------------


def test_sulpak_reads_availability_from_tile() -> None:
    """В плитке sulpak есть подпись статуса; «Нет в наличии» — не в наличии."""
    in_stock_html = """
    <div class="product__item product__item-js" data-name="Видеокарта Gigabyte RTX 5070 OC (GV-N5070AORUS-12GD)"
         data-code="1" data-price="450000.0" data-brand="Gigabyte">
      <div class="product__item-showcase">Есть в наличии</div>
    </div>
    """
    out_html = in_stock_html.replace("Есть в наличии", "Нет в наличии").replace("data-code=\"1\"", "data-code=\"2\"")

    offers = sulpak.parse(in_stock_html) + sulpak.parse(out_html)
    assert [o.in_stock for o in offers] == [True, False]
    assert offers[1].stock_note == "Нет в наличии"


def test_forcecom_reads_status_span() -> None:
    """Статус forcecom живёт в span.js-replace-status рядом с заголовком."""
    html = """
    <div itemtype="http://schema.org/Product">
      <div class="catalog-block__info-title">Видеокарта Gigabyte RTX 5070 (GV-N5070WF3OC-12GD)</div>
      <span class="js-replace-status instock">Есть в наличии</span>
      <meta itemprop="price" content="450000"/>
    </div>
    """
    out_html = html.replace("instock", "outstock").replace("Есть в наличии", "Нет в наличии")

    offers = forcecom.parse(html) + forcecom.parse(out_html)
    assert [o.in_stock for o in offers] == [True, False]


def test_forcecom_positive_default_without_status_node() -> None:
    """Нет статус-ноды — считаем в наличии: переименование классов не должно
    молча отключить все алерты."""
    html = """
    <div itemtype="http://schema.org/Product">
      <div class="catalog-block__info-title">Видеокарта Gigabyte RTX 5070 (GV-N5070WF3OC-12GD)</div>
      <meta itemprop="price" content="450000"/>
    </div>
    """
    assert all(o.in_stock for o in forcecom.parse(html))


def test_out_of_stock_is_saved_but_never_alerts(tmp_path, monkeypatch) -> None:
    """Цена «при заказе» 300 000 — не находка. В историю она попадает."""

    class FakeShop:
        SHOP = "fake"

        @staticmethod
        async def fetch(client):  # noqa: ANN001
            return [
                offer("fake", 300_000, in_stock=False, stock_note="При заказе", pn="GV-CHEAP"),
                offer("fake", 500_000, pn="GV-NORM"),
            ]

    monkeypatch.setitem(crawler.REGISTRY, "fake", FakeShop)
    db = tmp_path / "db.sqlite3"
    monkeypatch.setattr("gpudeals.storage.DB_PATH", db)

    findings, _, _ = asyncio.run(crawler.crawl(["fake"]))

    # Отсутствующий за 300 000 не будит, находящийся за 500 000 — обычная
    # новинка в бюджете, она алертит.
    assert [v.offer.price for v in findings] == [500_000]
    with connect(db) as conn:
        rows = {
            r["identity"]: r["in_stock"]
            for r in conn.execute("SELECT identity, in_stock FROM observations")
        }
    # Обе позиции в истории; отсутствующая — именно как отсутствующая.
    assert len(rows) == 2
    assert 0 in rows.values() and 1 in rows.values()


# --- медианы и минимумы только по наличию ----------------------------------


@pytest.fixture
def conn(tmp_path):
    with connect(tmp_path / "test.sqlite3") as connection:
        yield connection


def insert(conn, identity: str, price: int, shop: str, in_stock: int = 1) -> None:
    conn.execute(
        """INSERT INTO observations (observed_at, shop, kind, identity, title,
               price, url, class_key, chip, memory_gb, in_stock)
           VALUES (datetime('now'), ?, 'card', ?, 'тест', ?, 'https://e.kz',
                   'rtx5070-12', 'rtx5070', 12, ?)""",
        (shop, identity, price, in_stock),
    )


def test_class_prices_excludes_out_of_stock(conn) -> None:
    """Цена «под заказ» — не рыночная: она занижала бы медиану класса."""
    insert(conn, "i1", 450_000, "technodom")
    insert(conn, "i2", 470_000, "shop.kz")
    insert(conn, "i3", 200_000, "forcecom", in_stock=0)

    assert class_prices(conn, ItemKind.CARD, "rtx5070-12") == [450_000, 470_000]


def test_class_floor_excludes_out_of_stock(conn) -> None:
    insert(conn, "i1", 450_000, "technodom")
    insert(conn, "i3", 200_000, "forcecom", in_stock=0)

    assert class_floor_for_cards(conn) == {"rtx5070-12": 450_000}


# --- кросс-магазинное сравнение ---------------------------------------------


def test_cheaper_elsewhere_points_to_other_shop(conn) -> None:
    insert(conn, "b1", 450_000, "dns")

    verdict = evaluate(
        conn, offer("technodom", 480_000), Thresholds(),
        shop_minima={(ItemKind.CARD, "rtx5070-12"): {"technodom": 480_000, "dns": 450_000}},
    )
    assert verdict.cheaper_elsewhere == ("dns", 450_000)
    assert verdict.lowest_in_market is False


def test_lowest_in_market_when_nothing_cheaper(conn) -> None:
    insert(conn, "b1", 500_000, "dns")

    verdict = evaluate(
        conn, offer("technodom", 440_000), Thresholds(),
        shop_minima={(ItemKind.CARD, "rtx5070-12"): {"technodom": 440_000, "dns": 500_000}},
    )
    assert verdict.cheaper_elsewhere is None
    assert verdict.lowest_in_market is True


def test_same_shop_offer_is_not_cheaper_elsewhere(conn) -> None:
    """Дубль того же магазина — не «где дешевле», это та же витрина."""
    verdict = evaluate(
        conn, offer("technodom", 480_000), Thresholds(),
        shop_minima={(ItemKind.CARD, "rtx5070-12"): {"technodom": 450_000, "dns": 500_000}},
    )
    assert verdict.cheaper_elsewhere is None
    assert verdict.lowest_in_market is True


def test_report_shows_cross_shop_lines() -> None:
    from gpudeals.evaluate import Verdict

    expensive = Verdict(
        offer=offer("technodom", 480_000), signals=[(Signal.NEW_IN_BUDGET, "тест")],
        cheaper_elsewhere=("dns", 450_000),
    )
    assert "Дешевле сейчас: dns — 450 000 ₸ (−30 000 ₸)" in format_offer(expensive)

    cheapest = Verdict(offer=offer("dns", 440_000), signals=[(Signal.NEW_IN_BUDGET, "тест")])
    cheapest.lowest_in_market = True
    assert "Самая низкая цена среди магазинов" in format_offer(cheapest)


def test_crawl_overlay_counts_unsaved_shops(tmp_path, monkeypatch) -> None:
    """Магазины оцениваются до сохранения: цикл обязан видеть их офферы."""
    shops = {}

    class ShopA:
        SHOP = "shopa"

        @staticmethod
        async def fetch(client):  # noqa: ANN001
            return [offer("shopa", 480_000)]

    class ShopB:
        SHOP = "shopb"

        @staticmethod
        async def fetch(client):  # noqa: ANN001
            return [offer("shopb", 430_000)]

    shops["shopa"], shops["shopb"] = ShopA, ShopB
    for name, module in shops.items():
        monkeypatch.setitem(crawler.REGISTRY, name, module)
    monkeypatch.setattr("gpudeals.storage.DB_PATH", tmp_path / "db.sqlite3")

    findings, _, _ = asyncio.run(crawler.crawl(["shopa", "shopb"]))

    by_price = {v.offer.price: v for v in findings}
    expensive = by_price[480_000]
    assert expensive.cheaper_elsewhere == ("shopb", 430_000)
    assert by_price[430_000].lowest_in_market is True


# --- целевые цены ------------------------------------------------------------


def test_target_price_signal(conn) -> None:
    """Цена дошла до личной цели владельца — сигнал независимо от медиан."""
    verdict = evaluate(
        conn, offer("technodom", 355_000), Thresholds(),
        watch_targets={"rtx5070-12": 365_000},
    )
    assert Signal.TARGET_PRICE in [s for s, _ in verdict.signals]
    assert "365 000" in dict(verdict.signals)[Signal.TARGET_PRICE]


def test_target_price_above_goal_stays_silent(conn) -> None:
    verdict = evaluate(
        conn, offer("technodom", 400_000), Thresholds(),
        watch_targets={"rtx5070-12": 365_000},
    )
    assert Signal.TARGET_PRICE not in [s for s, _ in verdict.signals]


def test_target_price_flows_from_config(tmp_path, monkeypatch) -> None:
    class FakeShop:
        SHOP = "fake"

        @staticmethod
        async def fetch(client):  # noqa: ANN001
            return [offer("fake", 355_000)]

    config = Settings(
        watchlist=(WatchedModel("RTX 5070 12", "rtx5070-12", target_price=365_000),)
    )
    monkeypatch.setitem(crawler.REGISTRY, "fake", FakeShop)
    monkeypatch.setattr("gpudeals.storage.DB_PATH", tmp_path / "db.sqlite3")

    findings, _, _ = asyncio.run(crawler.crawl(["fake"], config=config))

    signals = [s for v in findings for s, _ in v.signals]
    assert Signal.TARGET_PRICE in signals
