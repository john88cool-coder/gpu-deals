"""Тесты парсера shop.kz на сохранённых страницах каталога."""

from __future__ import annotations

from pathlib import Path

import pytest

from gpudeals.models import ItemKind
from gpudeals.shops import shop_kz

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def offers():
    return shop_kz.parse((FIXTURES / "shopkz_rtx50.html").read_text(encoding="utf-8"))


def test_parses_rtx50_filter_page(offers) -> None:
    assert len(offers) >= 20
    assert all(offer.kind is ItemKind.CARD for offer in offers)
    assert all(offer.chip and offer.chip.startswith("rtx") for offer in offers)


def test_part_number_from_item_variant(offers) -> None:
    """У shop.kz партномер лежит в data-product как item_variant."""
    msi = next(o for o in offers if "Ventus 2X OC V1" in o.title)
    assert msi.part_number == "RTX50608GVENTUS2XOCV1"
    gigabyte = next(o for o in offers if "GV-N5050OC-8GL" in (o.part_number or ""))
    assert gigabyte.part_number == "GV-N5050OC-8GL"


def test_part_numbers_have_no_spaces(offers) -> None:
    """Один магазин пишет «GV-N5070EAGLE OC-12GD», другой — без пробела."""
    for offer in offers:
        if offer.part_number:
            assert " " not in offer.part_number


def test_old_price_and_current_price() -> None:
    """Позиция из общего каталога: 427 990 при заявленных 549 990."""
    offers = shop_kz.parse((FIXTURES / "shopkz_videocards.html").read_text(encoding="utf-8"))
    msi = next(o for o in offers if "RTX 5070 Ventus 3X OC" in o.title)
    assert msi.price == 427_990
    assert msi.shop_old_price == 549_990
    # Позиция без скидки не получает выдуманной старой цены.
    assert all(
        offer.shop_old_price is None or offer.shop_old_price > offer.price for offer in offers
    )


def test_availability_from_data_product(offers) -> None:
    assert all(offer.in_stock for offer in offers)


def test_full_catalog_pagination_metadata() -> None:
    html = (FIXTURES / "shopkz_videocards.html").read_text(encoding="utf-8")
    assert shop_kz.total_pages(html) == 58


def test_second_page_has_different_items() -> None:
    first = shop_kz.parse((FIXTURES / "shopkz_videocards.html").read_text(encoding="utf-8"))
    second = shop_kz.parse((FIXTURES / "shopkz_page2.html").read_text(encoding="utf-8"))
    assert {o.identity for o in first}.isdisjoint({o.identity for o in second})


def test_urls_are_absolute(offers) -> None:
    assert all(offer.url.startswith("https://shop.kz/") for offer in offers)
