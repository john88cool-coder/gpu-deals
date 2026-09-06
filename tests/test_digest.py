"""Тесты недельного дайджеста рынка.

Дайджест читает локальную базу (обхода не делает) и отвечает на три вопроса:
куда движутся цены по классам, что упало сильнее всех за неделю и где сейчас
максимум производительности за деньги.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gpudeals import benchmarks, crawler
from gpudeals.report import format_market_digest
from gpudeals.storage import connect

PM_CSV = """class_key,chip,model_name,passmark_g3d,desktop_rank
rtx5070-12,rtx5070,GeForce RTX 5070,28648,15
rtx5060ti-16,rtx5060ti,GeForce RTX 5060 Ti 16GB,22615,36
"""


@pytest.fixture
def conn(tmp_path, monkeypatch):
    csv_path = tmp_path / "pm.csv"
    csv_path.write_text(PM_CSV, encoding="utf-8")
    monkeypatch.setattr("gpudeals.benchmarks.BENCHMARKS_CSV", csv_path)
    benchmarks._ratings.cache_clear()
    with connect(tmp_path / "db.sqlite3") as connection:
        yield connection
    benchmarks._ratings.cache_clear()


def insert(conn, identity: str, price: int, days_ago: float, **overrides) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    fields = {
        "shop": "technodom",
        "title": f"Видеокарта {identity}",
        "url": "https://example.kz",
        "chip": "rtx5070",
        "class_key": "rtx5070-12",
        "in_stock": 1,
    }
    fields.update(overrides)
    conn.execute(
        """INSERT INTO observations (observed_at, shop, kind, identity, title,
               price, url, class_key, chip, memory_gb, in_stock)
           VALUES (?, ?, 'card', ?, ?, ?, ?, ?, ?, 12, ?)""",
        (stamp, fields["shop"], identity, fields["title"], price, fields["url"],
         fields["class_key"], fields["chip"], fields["in_stock"]),
    )


def test_medians_show_week_over_week_change(conn) -> None:
    """Медиана класса: минимум каждой позиции за окно, текущая и прошлые недели."""
    # RTX 5070: на этой неделе дешевле, чем на прошлой (3 позиции на класс).
    insert(conn, "a", 370_000, days_ago=10)
    insert(conn, "a", 350_000, days_ago=2)
    insert(conn, "b", 360_000, days_ago=10)
    insert(conn, "b", 355_000, days_ago=3)
    insert(conn, "d", 365_000, days_ago=10)
    insert(conn, "d", 350_000, days_ago=2)
    # Соседний класс вырос.
    for price_prev, price_now, name in [(800_000, 850_000, "c"),
                                        (810_000, 850_000, "e"),
                                        (820_000, 850_000, "f")]:
        insert(conn, name, price_prev, days_ago=10, class_key="rtx5080-16", chip="rtx5080")
        insert(conn, name, price_now, days_ago=2, class_key="rtx5080-16", chip="rtx5080")

    data = crawler._market_digest(conn)

    medians = {ck: (now, prev) for ck, now, prev in data.medians}
    assert medians["rtx5070-12"] == (350_000, 365_000)
    assert medians["rtx5080-16"] == (850_000, 810_000)


def test_medians_ignore_sparse_and_out_of_stock(conn) -> None:
    """Меньше трёх позиций — не медиана, а шум; «под заказ» — не рыночная цена."""
    insert(conn, "a", 300_000, days_ago=2)
    insert(conn, "b", 400_000, days_ago=2, in_stock=0)

    data = crawler._market_digest(conn)
    assert data.medians == []


def test_best_deal_is_the_biggest_week_drop(conn) -> None:
    insert(conn, "palit", 345_000, days_ago=10, shop="dns",
           title="Palit RTX 5060 Ti Infinity 3 V1", url="https://dns/p/palit",
           class_key="rtx5060ti-16", chip="rtx5060ti")
    insert(conn, "palit", 318_490, days_ago=1, shop="dns",
           title="Palit RTX 5060 Ti Infinity 3 V1", url="https://dns/p/palit",
           class_key="rtx5060ti-16", chip="rtx5060ti")
    # Конкурент упал слабее.
    insert(conn, "gigabyte", 400_000, days_ago=10)
    insert(conn, "gigabyte", 380_000, days_ago=2)

    data = crawler._market_digest(conn)

    assert data.best_deal is not None
    assert data.best_deal.price == 318_490
    assert data.best_deal.prev_price == 345_000
    assert data.best_deal.shop == "dns"
    assert data.best_deal.drop_pct == pytest.approx(7.7, abs=0.1)


def test_best_deal_requires_prev_week_and_stock(conn) -> None:
    """Без прошлой недели сделки нет; отсутствующий товар не предложение."""
    insert(conn, "new", 300_000, days_ago=2)
    insert(conn, "old", 500_000, days_ago=10)
    insert(conn, "old", 300_000, days_ago=1, in_stock=0)

    data = crawler._market_digest(conn)
    assert data.best_deal is None


def test_value_leaders_use_passmark_scores(conn) -> None:
    """Цена за балл: последняя цена недели, делённая на балл чипа."""
    insert(conn, "a", 286_480, days_ago=1)  # 28 648 баллов → 10,0 ₸/балл
    insert(conn, "b", 226_150, days_ago=1, class_key="rtx5060ti-16", chip="rtx5060ti")
    # Старая цена не должна затирать свежую при выборе «последней».
    insert(conn, "a", 350_000, days_ago=5)

    data = crawler._market_digest(conn)
    leaders = [(v.title, v.per_point) for v in data.value_leaders]

    assert leaders[0][1] == pytest.approx(10.0, abs=0.1)
    assert leaders[1][1] == pytest.approx(10.0, abs=0.1)
    assert len(leaders) == 2


def test_format_market_digest_renders_all_sections(conn) -> None:
    insert(conn, "a", 350_000, days_ago=2)
    insert(conn, "b", 355_000, days_ago=2, shop="dns")
    insert(conn, "c", 360_000, days_ago=2, shop="sulpak")
    insert(conn, "palit", 318_490, days_ago=1, shop="dns",
           title="Palit <5060 Ti> & Infinity", url="https://dns/p/x",
           class_key="rtx5060ti-16", chip="rtx5060ti")
    insert(conn, "palit", 345_000, days_ago=9, shop="dns",
           title="Palit <5060 Ti> & Infinity", url="https://dns/p/x",
           class_key="rtx5060ti-16", chip="rtx5060ti")

    text = format_market_digest(crawler._market_digest(conn))

    assert "📊 Дайджест рынка за неделю" in text
    assert "Медианы классов" in text
    assert "rtx5070-12: 355 000 ₸ (—" in text, "нет прошлой недели — прочерк"
    assert "Лучшее предложение недели" in text
    # Экранирование HTML: заголовок магазина приходит с <, &.
    assert "Palit &lt;5060 Ti&gt; &amp; Infinity" in text
    assert "(dns)" in text
    assert "−8% за неделю" in text
    assert "Лидеры по цене за балл" in text


def test_digest_command_reads_db_without_crawling(tmp_path, monkeypatch) -> None:
    """digest не должен поднимать обход магазинов — как heartbeat."""

    def fail_if_crawled(*args, **kwargs):  # pragma: no cover
        raise AssertionError("digest не должен запускать обход")

    monkeypatch.setattr(crawler.asyncio, "run", fail_if_crawled)

    class Recording:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    notifier = Recording()
    crawler.send_digest(notifier)

    assert len(notifier.sent) == 1
    assert "Дайджест рынка" in notifier.sent[0]
    # Пустая база — вежливый текст, а не исключение.
    assert "Медиан пока нет" in notifier.sent[0]
