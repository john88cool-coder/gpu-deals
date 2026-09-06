"""Тесты рестока, минимумов месяца в дайджесте и catch-up обхода.

Ресток: позиция вернулась на витрину по цене ≤ личной цели. Прошлый алерт мог
быть дешевле — обычная дедупликация is_new_low подавила бы эту новость, поэтому
для рестока она обходится.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from gpudeals import backtest, benchmarks, crawler
from gpudeals.config import Settings, WatchedModel
from gpudeals.evaluate import Signal, evaluate
from gpudeals.models import ItemKind, Offer
from gpudeals.storage import connect


def offer(price: int, in_stock: bool = True) -> Offer:
    return Offer(
        shop="fake",
        kind=ItemKind.CARD,
        title="Видеокарта RTX 5070",
        price=price,
        url="https://example.kz",
        class_key="rtx5070-12",
        chip="rtx5070",
        memory_gb=12,
        part_number="GV-1",
        in_stock=in_stock,
    )


def insert(conn, identity: str, price: int, days_ago: float, in_stock: int = 1,
           class_key: str = "rtx5070-12", chip: str = "rtx5070", shop: str = "fake") -> None:
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO observations (observed_at, shop, kind, identity, title,
               price, url, class_key, chip, memory_gb, in_stock)
           VALUES (?, ?, 'card', ?, 'тест', ?, 'https://e.kz', ?, ?, 12, ?)""",
        (stamp, shop, identity, price, class_key, chip, in_stock),
    )


# --- ресток -----------------------------------------------------------------


def test_restock_signal_when_back_in_stock_under_target(tmp_path) -> None:
    with connect(tmp_path / "db.sqlite3") as conn:
        insert(conn, "fake:pn:gv-1", 500_000, days_ago=2, in_stock=0)
        verdict = evaluate(
            conn, offer(294_000), Settings().thresholds,
            watch_targets={"rtx5070-12": 295_000},
        )
    signals = [s for s, _ in verdict.signals]
    assert Signal.RESTOCK in signals


def test_restock_above_target_stays_silent(tmp_path) -> None:
    with connect(tmp_path / "db.sqlite3") as conn:
        insert(conn, "fake:pn:gv-1", 500_000, days_ago=2, in_stock=0)
        verdict = evaluate(
            conn, offer(400_000), Settings().thresholds,
            watch_targets={"rtx5070-12": 295_000},
        )
    assert Signal.RESTOCK not in [s for s, _ in verdict.signals]


def test_restock_bypasses_dedup_gate(tmp_path, monkeypatch) -> None:
    """Прошлый алерт был дешевле, но «вернулся в наличие по цели» — новость."""
    db = tmp_path / "db.sqlite3"
    monkeypatch.setattr("gpudeals.storage.DB_PATH", db)

    with connect(db) as conn:
        insert(conn, "fake:pn:gv-1", 280_000, days_ago=6, in_stock=0)
    # Раньше алертовали по 270 000; ресток по 294 000 дороже — is_new_low
    # говорит «нет», ресток обязан пройти.

    class FakeShop:
        SHOP = "fake"

        @staticmethod
        async def fetch(client):  # noqa: ANN001
            return [offer(294_000)]

    monkeypatch.setitem(crawler.REGISTRY, "fake", FakeShop)
    config = Settings(watchlist=(WatchedModel("RTX 5070 12", "rtx5070-12",
                                              target_price=295_000),))

    findings, _, _ = asyncio.run(crawler.crawl(["fake"], config=config))

    signals = [s for v in findings for s, _ in v.signals]
    assert Signal.RESTOCK in signals


def test_in_stock_without_gap_is_not_restock(tmp_path) -> None:
    """Позиция была в наличии и осталась — рестоком это не является."""
    with connect(tmp_path / "db.sqlite3") as conn:
        insert(conn, "fake:pn:gv-1", 290_000, days_ago=2, in_stock=1)
        verdict = evaluate(
            conn, offer(294_000), Settings().thresholds,
            watch_targets={"rtx5070-12": 295_000},
        )
    assert Signal.RESTOCK not in [s for s, _ in verdict.signals]


# --- минимумы месяца в дайджесте --------------------------------------------


def test_digest_monthly_minima_with_shop(tmp_path) -> None:
    with connect(tmp_path / "db.sqlite3") as conn:
        insert(conn, "a", 294_590, days_ago=10, shop="sulpak")
        insert(conn, "b", 380_000, days_ago=5, shop="dns")
        insert(conn, "c", 250_000, days_ago=8, in_stock=0, shop="forcecom")

        data = crawler._market_digest(conn)

    entry = next(e for e in data.monthly_minima if e[0] == "rtx5070-12")
    assert entry[1] == 294_590 and entry[2] == "sulpak"
    # Доля наличия: одна отсутствующая из трёх позиций класса.
    assert entry[3] == pytest.approx(2 / 3)
    # Отсутствующий товар — не рыночный минимум.
    assert all(price != 250_000 for _, price, _, _ in data.monthly_minima)


def test_digest_renders_monthly_minima(tmp_path) -> None:
    from gpudeals.report import format_market_digest

    with connect(tmp_path / "db.sqlite3") as conn:
        insert(conn, "a", 294_590, days_ago=10, shop="sulpak")
        data = crawler._market_digest(conn)

    text = format_market_digest(data)
    assert "Минимумы за месяц наблюдений" in text
    assert "rtx5070-12: 294 590 ₸ (sulpak)" in text


# --- catch-up ----------------------------------------------------------------


def test_crawl_skips_when_database_is_fresh(tmp_path, monkeypatch) -> None:
    """Свежая база — тихий выход без единого запроса к магазинам."""
    db = tmp_path / "db.sqlite3"
    monkeypatch.setattr("gpudeals.storage.DB_PATH", db)

    def fail_if_fetched(*args, **kwargs):  # pragma: no cover
        raise AssertionError("свежая база не должна приводить к обходу")

    class FakeShop:
        SHOP = "fake"
        fetch = staticmethod(fail_if_fetched)

    monkeypatch.setitem(crawler.REGISTRY, "fake", FakeShop)

    with connect(db) as conn:
        insert(conn, "fake:pn:gv-1", 400_000, days_ago=0.1)

    findings, breakages, summary = asyncio.run(
        crawler.crawl(["fake"], stale_hours=4)
    )
    assert (findings, breakages, summary) == ([], [], [])


def test_crawl_runs_when_database_is_stale(tmp_path, monkeypatch) -> None:
    db = tmp_path / "db.sqlite3"
    monkeypatch.setattr("gpudeals.storage.DB_PATH", db)

    class FakeShop:
        SHOP = "fake"

        @staticmethod
        async def fetch(client):  # noqa: ANN001
            return [offer(400_000)]

    monkeypatch.setitem(crawler.REGISTRY, "fake", FakeShop)

    with connect(db) as conn:
        insert(conn, "fake:pn:gv-1", 400_000, days_ago=5)

    _, _, summary = asyncio.run(crawler.crawl(["fake"], stale_hours=4))
    assert summary == [("fake", 1, True)]


# --- бэктест -----------------------------------------------------------------


def test_backtest_counts_fires_and_reports(tmp_path) -> None:
    with connect(tmp_path / "db.sqlite3") as conn:
        # Ровная история по 400 000, затем день с падением до 300 000:
        # тренд наберёт 7 точек, упало — 25%.
        for day in range(7, 14):
            insert(conn, "i1", 400_000, days_ago=day)
        insert(conn, "i1", 300_000, days_ago=1)
        # Две «соседние» позиции дают классу медиану.
        for name in ("i2", "i3", "i4"):
            for day in range(7, 14):
                insert(conn, name, 400_000, days_ago=day)
            insert(conn, name, 400_000, days_ago=1)

        report = backtest.run(conn, days=30)

    assert "упало ≥2%" in report
    assert "300 000 ₸" in report or "300 000" in report
    assert "Срабатывания при текущих порогах" in report


def test_backtest_on_empty_history(tmp_path) -> None:
    with connect(tmp_path / "db.sqlite3") as conn:
        assert "Истории пока нет" in backtest.run(conn)


def test_dashboard_renders_recent_alerts(tmp_path, monkeypatch) -> None:
    """Дашборд показывает, что бот сообщал за последнее время."""
    from gpudeals.dashboard import render

    csv_path = tmp_path / "pm.csv"
    csv_path.write_text(
        "class_key,chip,model_name,passmark_g3d,desktop_rank\n"
        "rtx5070-12,rtx5070,GeForce RTX 5070,28648,15\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gpudeals.benchmarks.BENCHMARKS_CSV", csv_path)
    benchmarks._ratings.cache_clear()

    db = tmp_path / "db.sqlite3"
    with connect(db) as conn:
        insert(conn, "fake:pn:gv-1", 363_990, days_ago=0.1)
        conn.execute(
            "INSERT INTO alerts (identity, alerted_at, alerted_price)"
            " VALUES ('fake:pn:gv-1', '2026-09-06T10:00:00+00:00', 363990)"
        )
        out = tmp_path / "site" / "index.html"
        render(conn, out)
    benchmarks._ratings.cache_clear()

    html = out.read_text(encoding="utf-8")
    assert "Последние алерты бота" in html
    assert "363 990 ₸" in html
    assert "2026-09-06 10:00" in html


def test_dashboard_without_alerts_skips_section(tmp_path) -> None:
    from gpudeals.dashboard import render

    db = tmp_path / "db.sqlite3"
    with connect(db) as conn:
        insert(conn, "fake:pn:gv-1", 363_990, days_ago=0.1)
        out = tmp_path / "site" / "index.html"
        render(conn, out)

    assert "Последние алерты бота" not in out.read_text(encoding="utf-8")
