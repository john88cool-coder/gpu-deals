"""Общая для магазинов защита от повторов внутри одного обхода.

Проверено на живых страницах: каталог sulpak из 5 страниц на запрос `?page=7`
отдаёт последнюю доступную вместо пустой, а forcecom переставляет товары между
страницами между запросами. И то и другое писало копии одного снимка в историю
цен.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gpudeals.models import ItemKind, Offer
from gpudeals.shops import forcecom, sulpak
from gpudeals.shops.paging import new_offers

FIXTURES = Path(__file__).parent / "fixtures"


def offer(identity_source: str, price: int = 100_000) -> Offer:
    """Позиция с предсказуемым identity: партномер задаёт ключ."""
    return Offer(
        shop="sulpak",
        kind=ItemKind.CARD,
        title=f"Видеокарта {identity_source}",
        price=price,
        url="https://example.kz",
        part_number=identity_source,
    )


def test_new_offers_keeps_first_occurrence() -> None:
    seen: set[str] = set()
    first = new_offers([offer("GV-1", 100), offer("GV-2", 200)], seen)
    assert [o.part_number for o in first] == ["GV-1", "GV-2"]

    # Повтор того же партномера с другой ценой не проходит: это тот же товар,
    # показанный магазином второй раз.
    second = new_offers([offer("GV-2", 999), offer("GV-3", 300)], seen)
    assert [o.part_number for o in second] == ["GV-3"]
    assert seen == {"sulpak:pn:gv-1", "sulpak:pn:gv-2", "sulpak:pn:gv-3"}


def test_new_offers_dedupes_within_one_page() -> None:
    """Одна страница тоже может содержать позицию дважды."""
    seen: set[str] = set()
    assert len(new_offers([offer("GV-1"), offer("GV-1")], seen)) == 1


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class ClampingClient:
    """Магазин, который на номер за границей каталога отдаёт последнюю страницу.

    Так ведёт себя sulpak: при пяти страницах `?page=7` возвращает страницу 3
    с теми же карточками.
    """

    def __init__(self, pages: dict[int, str], last_page: int) -> None:
        self._pages = pages
        self._last_page = last_page
        self.requested: list[int] = []

    async def get(self, url: str, params: dict | None = None) -> FakeResponse:
        page = int((params or {}).get("page", 1))
        self.requested.append(page)
        return FakeResponse(self._pages[min(page, self._last_page)])


def test_sulpak_total_pages_from_pagination() -> None:
    html = (FIXTURES / "sulpak_videocards.html").read_text(encoding="utf-8")
    assert sulpak.total_pages(html) == 5


def test_sulpak_total_pages_without_pagination_block() -> None:
    assert sulpak.total_pages("<div>без пагинации</div>") == 1


def test_sulpak_fetch_stops_at_catalog_end_and_writes_no_copies() -> None:
    """Ключевая регрессия: 10 запросов при 2 страницах давали 8 копий снимка."""
    import asyncio

    page1 = (FIXTURES / "sulpak_videocards.html").read_text(encoding="utf-8")
    page2 = (FIXTURES / "sulpak_page2.html").read_text(encoding="utf-8")
    # В фикстуре page1 пагинация заявляет 5 страниц, но магазин отдаёт только 2:
    # запрос страницы 3 возвращает страницу 2 повторно.
    client = ClampingClient({1: page1, 2: page2}, last_page=2)

    offers = asyncio.run(sulpak.fetch(client))

    identities = [o.identity for o in offers]
    assert len(identities) == len(set(identities)), "снимок записан с копиями"
    # Часть позиций второй страницы совпадает с первой — берётся первая встреча.
    assert set(identities) >= {o.identity for o in sulpak.parse(page1)}
    assert set(identities) <= {o.identity for o in sulpak.parse(page1)} | {
        o.identity for o in sulpak.parse(page2)
    }
    # Обход прекращается на первой странице без новых позиций, а не идёт до 10.
    assert client.requested == [1, 2, 3]


class RepeatingClient:
    """Магазин, который показывает одни и те же товары на разных страницах."""

    def __init__(self, page1: str, page2: str) -> None:
        self._pages = {1: page1, 2: page2}

    async def get(self, url: str, params: dict | None = None) -> FakeResponse:
        page = int((params or {}).get("PAGEN_1", 1))
        return FakeResponse(self._pages.get(page, self._pages[1]))


def test_forcecom_fetch_drops_products_repeated_across_pages(monkeypatch) -> None:
    """Товар, попавший и на страницу 1, и на страницу 2, учитывается один раз."""
    import asyncio

    monkeypatch.setattr(forcecom, "CRAWL_DELAY", 0.0)
    monkeypatch.setattr(forcecom, "_MAX_PAGES", 2)

    page1 = (FIXTURES / "forcecom_cards.html").read_text(encoding="utf-8")
    # Вторая страница — та же самая: крайний случай полного повтора.
    offers = asyncio.run(forcecom.fetch(RepeatingClient(page1, page1)))

    identities = [o.identity for o in offers]
    assert len(identities) == len(set(identities))
    assert set(identities) == {o.identity for o in forcecom.parse(page1)}
