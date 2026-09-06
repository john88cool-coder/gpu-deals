"""Тесты сторожа и стабильных identity.

Сторож закрывает последний сценарий тихой смерти: если обходы перестали
приходить (парсер сломался, отправка упала, workflow отключили), тишина в
Telegram неотличима от «скидок нет» — отдельная проверка будит владельца.
Стабильные SKU защищают историю цен от правок заголовков магазинами.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gpudeals import crawler
from gpudeals.shops import alfa
from gpudeals.storage import connect, record_crawl


def insert_crawl(conn, shop: str, hours_ago: float, ok: int = 1) -> None:
    stamp = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO crawls (started_at, shop, item_count, ok) VALUES (?, ?, 40, ?)",
        (stamp, shop, ok),
    )


class Recording:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):  # noqa: ANN001
        self.sent.append(text)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Изолированная база: connect() без пути берёт DB_PATH из конфига."""
    path = tmp_path / "db.sqlite3"
    monkeypatch.setattr("gpudeals.storage.DB_PATH", path)
    return path


def test_watchdog_alerts_on_stale_crawl(db) -> None:
    with connect(db) as conn:
        insert_crawl(conn, "technodom", hours_ago=30)
        insert_crawl(conn, "dns", hours_ago=2)

    notifier = Recording()
    assert crawler.send_watchdog(notifier, max_age_hours=12, shops=["technodom", "dns"]) is True
    assert "technodom: последний успешный обход 30 ч назад" in notifier.sent[0]
    assert "dns" not in notifier.sent[0]


def test_watchdog_silent_when_all_fresh(db) -> None:
    with connect(db) as conn:
        insert_crawl(conn, "technodom", hours_ago=2)

    notifier = Recording()
    assert crawler.send_watchdog(notifier, max_age_hours=12, shops=["technodom"]) is False
    assert notifier.sent == []


def test_watchdog_flags_shop_without_any_crawl(db) -> None:
    with connect(db) as conn:
        insert_crawl(conn, "technodom", hours_ago=1)

    notifier = Recording()
    crawler.send_watchdog(notifier, max_age_hours=12, shops=["technodom", "kaspi"])

    assert "kaspi: успешных обходов не зафиксировано" in notifier.sent[0]


def test_watchdog_ignores_failed_crawls(tmp_path) -> None:
    """Неуспешный обход не продлевает свежесть: сторож видит только ok=1."""
    with connect(tmp_path / "db.sqlite3") as conn:
        insert_crawl(conn, "technodom", hours_ago=30)
        insert_crawl(conn, "technodom", hours_ago=0.5, ok=0)

    notifier = Recording()
    assert crawler.send_watchdog(notifier, max_age_hours=12, shops=["technodom"]) is True


# --- стабильные identity из ссылок ------------------------------------------


def test_forcecom_sku_from_model_url() -> None:
    """У части позиций forcecom нет партномера — identity держалась на
    заголовке, и его правка обнуляла историю цены. Слаг ссылки стабилен."""
    from gpudeals.shops import forcecom

    base = """
    <div itemtype="http://schema.org/Product">
      <div class="catalog-block__info-title">{title}</div>
      <a href="/catalog/desktops/model/486920/">×</a>
      <span class="js-replace-status instock">Есть в наличии</span>
      <meta itemprop="price" content="450000"/>
    </div>
    """
    html_v1 = base.format(title="Системный блок LogyCom PBA (RTX 5060 Ti)")
    html_v2 = base.format(title="Системный блок LogyCom PBA PRO (RTX 5060 Ti), 16 ГБ")

    o1 = forcecom.parse(html_v1)[0]
    o2 = forcecom.parse(html_v2)[0]

    assert o1.sku == "model-486920"
    # Магазин правит название — identity не меняется, история цены живёт.
    assert o1.identity == o2.identity == "forcecom:sku:model-486920"


def test_alfa_sku_from_listing_id() -> None:
    from gpudeals.shops import alfa as alfa_shop

    offers = alfa_shop.parse(
        (Path(__file__).parent / "fixtures" / "alfa_cards.html").read_text(encoding="utf-8")
    )
    assert all(o.sku for o in offers)
    # identity: партномер, если извлёкся из названия; иначе — стабильный SKU.
    expected = {
        f"alfa:pn:{o.part_number.lower()}" if o.part_number else f"alfa:sku:{o.sku}"
        for o in offers
    }
    assert {o.identity for o in offers} == expected
