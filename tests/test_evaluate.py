"""Тесты оценки: магазинная скидка не является сигналом, тренд вместо минимума."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gpudeals.config import Thresholds
from gpudeals.evaluate import Signal, evaluate, is_new_low
from gpudeals.models import ItemKind, Offer
from gpudeals.storage import SCHEMA, connect, record_alert


@pytest.fixture
def conn(tmp_path):
    with connect(tmp_path / "test.sqlite3") as connection:
        yield connection


def make_offer(**overrides) -> Offer:
    base = dict(
        shop="technodom",
        kind=ItemKind.CARD,
        title="Видеокарта Gigabyte RTX 5070 WINDFORCE OC 12GB",
        price=457_990,
        url="https://www.technodom.kz/p/test",
        class_key="rtx5070-12",
        part_number="GV-N5070WF3OC-12GD",
        chip="rtx5070",
        memory_gb=12,
        brand="Gigabyte",
    )
    return Offer(**{**base, **overrides})


def seed_history(conn, identity: str, prices: list[int], days_ago_start: int = 10) -> None:
    """Записывает наблюдения задним числом."""
    for index, price in enumerate(prices):
        stamp = (
            datetime.now(UTC) - timedelta(days=days_ago_start, hours=-index * 6)
        ).isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO observations (observed_at, shop, kind, identity, title,
                   price, url, class_key, part_number, chip, memory_gb, in_stock)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                stamp, "technodom", ItemKind.CARD.value, identity, "тест",
                price, "https://example.kz", "rtx5070-12", "GV-N5070WF3OC-12GD",
                "rtx5070", 12,
            ),
        )


def seed_class_peers(conn, class_key: str, prices: list[int], kind=ItemKind.CARD) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    for index, price in enumerate(prices):
        conn.execute(
            """INSERT INTO observations (observed_at, shop, kind, identity, title,
                   price, url, class_key, chip, memory_gb, in_stock)
               VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
            (
                stamp, "shop", kind.value, f"peer-{class_key}-{index}", "аналог",
                price, "https://example.kz", class_key, "rtx5070", 12,
            ),
        )


def test_inflated_shop_discount_alone_produces_no_signal(conn) -> None:
    """ASUS PRIME RTX 5070 Ti: магазин рисует -41%, но цена выше аналогов."""
    seed_class_peers(conn, "rtx5070ti-16", [615_990, 665_990, 669_990, 620_000])
    offer = make_offer(
        title="Видеокарта ASUS PRIME RTX 5070 Ti OC Edition 16GB",
        price=757_990,
        class_key="rtx5070ti-16",
        chip="rtx5070ti",
        memory_gb=16,
        part_number=None,
        shop_old_price=1_287_990,
        shop_discount_pct=41,
    )
    verdict = evaluate(conn, offer, Thresholds())
    assert verdict.signals == []
    assert verdict.over_budget_by == 757_990 - 600_000


def test_below_class_median_triggers_signal(conn) -> None:
    """Technodom 615 990 против минимума Kaspi 645 990 — настоящая находка."""
    seed_class_peers(conn, "rtx5070ti-16", [645_990, 665_990, 669_990, 673_900])
    offer = make_offer(
        title="Видеокарта Gigabyte RTX 5070 Ti WINDFORCE SFF 16GB",
        price=615_990,
        class_key="rtx5070ti-16",
        chip="rtx5070ti",
        memory_gb=16,
        part_number=None,
    )
    verdict = evaluate(conn, offer, Thresholds())
    assert Signal.BELOW_CLASS in [signal for signal, _ in verdict.signals]


def test_price_drop_uses_trend_not_all_time_minimum(conn) -> None:
    """На растущем рынке минимум прошлого не должен заглушать сигнал."""
    offer = make_offer(price=470_000)
    # Цена росла: минимум 400 000 давно, свежая медиана около 500 000.
    seed_history(conn, offer.identity, [400_000, 430_000, 480_000, 500_000, 505_000, 510_000, 515_000])
    verdict = evaluate(conn, offer, Thresholds())
    signals = [signal for signal, _ in verdict.signals]
    assert Signal.PRICE_DROP in signals


def test_no_drop_signal_before_enough_observations(conn) -> None:
    offer = make_offer(price=400_000)
    seed_history(conn, offer.identity, [500_000, 500_000])
    verdict = evaluate(conn, offer, Thresholds())
    assert Signal.PRICE_DROP not in [signal for signal, _ in verdict.signals]


def test_class_median_separates_cards_from_builds(conn) -> None:
    """Сборка за 870 000 не должна задирать медиану класса для карт."""
    seed_class_peers(conn, "rtx5070-12", [441_990, 457_990, 464_990, 470_990])
    seed_class_peers(conn, "rtx5070-12", [870_000, 1_249_990, 1_399_990, 1_300_000], kind=ItemKind.BUILD)
    offer = make_offer(price=410_990)
    verdict = evaluate(conn, offer, Thresholds())
    assert verdict.class_median is not None
    assert verdict.class_median < 500_000


def test_build_residual_subtracts_bare_card_price(conn) -> None:
    """IT-MR с RTX 5070 за 870 000 при карте за 457 990 — остаток 412 010."""
    build = make_offer(
        kind=ItemKind.BUILD,
        title="IT-MR i5-14400F / RTX 5070 12 Гб / 32 Гб / SSD 1000 Гб / Win 11",
        price=870_000,
        part_number=None,
    )
    verdict = evaluate(conn, build, Thresholds(), {"rtx5070-12": 457_990})
    assert verdict.build_residual == 412_010
    assert verdict.over_budget_by is None  # потолок сборки — 1 000 000


def test_build_over_its_own_budget(conn) -> None:
    build = make_offer(
        kind=ItemKind.BUILD,
        title="TD GARANT R7 7800X3D / RTX 5070 12 Гб / 32 Гб / SSD 1024 Гб",
        price=1_249_990,
        part_number=None,
    )
    verdict = evaluate(conn, build, Thresholds())
    assert verdict.over_budget_by == 249_990


def test_repeat_alert_only_on_new_low(conn) -> None:
    offer = make_offer(price=450_000)
    record_alert(conn, offer.identity, 450_000)
    assert is_new_low(conn, offer) is False
    assert is_new_low(conn, make_offer(price=440_000)) is True


def test_prune_removes_only_old_rows(conn) -> None:
    from datetime import datetime, timedelta, timezone

    from gpudeals.storage import prune_old_observations

    fresh = make_offer()
    seed_history(conn, fresh.identity, [500_000, 490_000], days_ago_start=2)

    stale_identity = "technodom:pn:OLD-1"
    old_stamp = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO observations (observed_at, shop, kind, identity, title,
               price, url, class_key, chip, memory_gb, in_stock)
           VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
        (old_stamp, "technodom", ItemKind.CARD.value, stale_identity, "старое",
         100_000, "https://example.kz", "rtx5070-12", "rtx5070", 12),
    )
    record_alert(conn, stale_identity, 100_000)

    removed = prune_old_observations(conn, days=30)
    assert removed == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM observations WHERE identity = ?", (stale_identity,)
    ).fetchone()[0] == 0
    # Алерт без наблюдений тоже уходит.
    assert conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE identity = ?", (stale_identity,)
    ).fetchone()[0] == 0
    # Свежие данные не тронуты.
    assert conn.execute(
        "SELECT COUNT(*) FROM observations WHERE identity = ?", (fresh.identity,)
    ).fetchone()[0] == 2
