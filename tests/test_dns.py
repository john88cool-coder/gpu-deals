"""Тесты парсера dns-shop.kz на отрендеренной браузером странице."""

from __future__ import annotations

from pathlib import Path

import pytest

from gpudeals.models import ItemKind
from gpudeals.shops import dns

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def offers():
    return dns.parse((FIXTURES / "dns_rendered.html").read_text(encoding="utf-8"))


def test_parses_rendered_catalog(offers) -> None:
    assert len(offers) >= 15
    assert all(offer.kind is ItemKind.CARD for offer in offers)


def test_price_and_title(offers) -> None:
    palit = next(o for o in offers if "RTX 5090 GameRock" in o.title)
    assert palit.price == 2_488_990
    assert palit.chip == "rtx5090"
    assert palit.memory_gb == 32


def test_part_number_from_title_brackets(offers) -> None:
    palit = next(o for o in offers if "RTX 5090 GameRock" in o.title)
    assert palit.part_number == "NE75090019R5-GB2020G"


def test_availability_is_parsed(offers) -> None:
    statuses = {offer.stock_note for offer in offers if offer.stock_note}
    assert statuses, "в выборке должны быть метки наличия"
    assert all(isinstance(offer.in_stock, bool) for offer in offers)


def test_urls_are_absolute(offers) -> None:
    assert all(offer.url.startswith("https://www.dns-shop.kz/") for offer in offers)


def test_no_old_price_in_listing(offers) -> None:
    assert all(offer.shop_old_price is None for offer in offers)


def test_total_pages_from_title() -> None:
    html = (FIXTURES / "dns_rendered.html").read_text(encoding="utf-8")
    assert dns.total_pages(html) == 14
