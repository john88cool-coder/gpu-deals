"""Тесты свода «самая выгодная в каждой группе».

Свод приходит три раза в день (09:00/14:00/20:00 Алматы). «Выгодная» —
минимальная из ПОСЛЕДНИХ цен позиций класса: минимум за окно мог быть три
дня назад по позиции, которой уже нет или которая подорожала.
"""

from __future__ import annotations

import sqlite3

import pytest

from gpudeals import crawler
from gpudeals.config import INTERESTED_CHIPS
from gpudeals.report import format_best_deals
from gpudeals.storage import class_best_deals, connect


class Recording:
    def __init__(self):
        self.sent = []
        self.buttons = []

    def send(self, text, buttons=None):  # noqa: ANN001
        self.sent.append(text)
        self.buttons.append(buttons)


def insert(conn, identity: str, price: int, shop: str, days_ago: float = 0.0,
           class_key: str = "rtx5070-12", chip: str = "rtx5070",
           in_stock: int = 1, title: str = "Видеокарта RTX 5070") -> None:
    from datetime import UTC, datetime, timedelta
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO observations (observed_at, shop, kind, identity, title,
               price, url, class_key, chip, memory_gb, in_stock)
           VALUES (?, ?, 'card', ?, ?, ?, 'https://e.kz/1', ?, ?, 12, ?)""",
        (stamp, shop, identity, title, price, class_key, chip, in_stock),
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr("gpudeals.storage.DB_PATH", tmp_path / "db.sqlite3")
    return tmp_path / "db.sqlite3"


def test_best_deals_uses_latest_price_not_window_minimum(db) -> None:
    """Минимум за окно принадлежал позиции, подорожавшей к сейчас: свод
    обязан показать последнюю дешёвую, а не протухший минимум."""
    with connect(db) as conn:
        insert(conn, "a", 300_000, "dns", days_ago=1.5, title="дешёвая вчера")
        insert(conn, "a", 380_000, "dns", days_ago=0.1, title="уже дороже")
        insert(conn, "b", 340_000, "sulpak", days_ago=0.1, title="актуальная")

        deals = class_best_deals(conn, INTERESTED_CHIPS, 8)

    assert deals == [("rtx5070-12", 340_000, "sulpak",
                      "актуальная", "https://e.kz/1")]


def test_best_deals_skip_out_of_stock_and_small_memory(db) -> None:
    with connect(db) as conn:
        insert(conn, "a", 250_000, "dns", in_stock=0, title="нет в наличии")
        insert(conn, "c", 260_000, "dns", title="мало памяти",
               class_key="rtx5060-8", chip="rtx5060")
        insert(conn, "b", 340_000, "sulpak", title="годная")

        deals = class_best_deals(conn, INTERESTED_CHIPS, 8)

    assert [d[3] for d in deals] == ["годная"]


def test_best_deals_one_per_class_picks_cheapest(db) -> None:
    with connect(db) as conn:
        insert(conn, "x1", 400_000, "dns", title="дороже")
        insert(conn, "x2", 350_000, "sulpak", title="дешевле")

        deals = class_best_deals(conn, INTERESTED_CHIPS, 8)

    assert len(deals) == 1
    assert deals[0][1] == 350_000 and deals[0][2] == "sulpak"


def test_send_best_deals_renders_context_and_buttons(db, monkeypatch) -> None:
    with connect(db) as conn:
        # Три соседа дают медиану: 340 000 — медианный уровень.
        for name, price in [("a", 363_990), ("b", 370_000), ("c", 380_000),
                            ("d", 340_000)]:
            insert(conn, name, price, "sulpak", title=f"RTX 5070 {name}")

    notifier = Recording()
    crawler.send_best_deals(notifier)

    text = notifier.sent[0]
    assert "Самые выгодные по группам" in text
    assert "🎯 <b>RTX 5070</b> — <b>340 000 ₸</b> · sulpak" in text
    assert "дешевле медианы" in text
    assert "цель 365 000 ₸ ✓ достигнута" in text, "340 000 ≤ 365 000"
    # Кнопки: по строке на каждую группу.
    assert notifier.buttons[0] == [[("RTX 5070 · 340 000 ₸ · sulpak", "https://e.kz/1")]]


def test_send_best_deals_includes_build(db) -> None:
    with connect(db) as conn:
        insert(conn, "card", 400_000, "technodom")
        conn.execute(
            """INSERT INTO observations (observed_at, shop, kind, identity, title,
                   price, url, class_key, chip, memory_gb, in_stock)
               VALUES (datetime('now'), 'technodom', 'build', 'b1',
                       'Компьютер RTX 5070', 870000, 'https://e.kz',
                       'rtx5070-12', 'rtx5070', 12, 1)"""
        )

    notifier = Recording()
    crawler.send_best_deals(notifier)

    assert "🧱 <b>Сборка</b> RTX 5070" in notifier.sent[0]
    assert "остаток за платформу 470 000 ₸" in notifier.sent[0]


def test_format_best_deals_escapes_titles() -> None:
    text = format_best_deals(
        [("rtx5070-12", 400_000, "dns", "RTX 5070 <OC> & 12GB",
          "https://e.kz/1?a=1&b=2")],
        {}, {},
    )
    assert "RTX 5070 &lt;OC&gt; &amp; 12GB" in text
    assert "400 000 ₸" in text
