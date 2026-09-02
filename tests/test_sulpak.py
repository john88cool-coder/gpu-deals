"""Тесты парсера sulpak.kz."""

from __future__ import annotations

from pathlib import Path

import pytest

from gpudeals.models import ItemKind
from gpudeals.shops import sulpak

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def offers():
    return sulpak.parse((FIXTURES / "sulpak_videocards.html").read_text(encoding="utf-8"))


def test_parses_catalog(offers) -> None:
    assert len(offers) >= 15
    assert all(offer.kind is ItemKind.CARD for offer in offers)


def test_data_attributes_are_source_of_truth(offers) -> None:
    gigabyte = next(o for o in offers if "GV-N507TWF3OCV2-16GD" in o.title)
    assert gigabyte.price == 659_990
    assert gigabyte.brand == "Gigabyte"
    assert gigabyte.sku == "650821"
    assert gigabyte.class_key == "rtx5070ti-16"


def test_part_number_from_parentheses() -> None:
    """Palit стоит на второй странице каталога."""
    offers = sulpak.parse((FIXTURES / "sulpak_page2.html").read_text(encoding="utf-8"))
    palit = next(o for o in offers if "NE7506T019P1-GB2062D" in o.title)
    assert palit.part_number == "NE7506T019P1-GB2062D"
    assert palit.price == 258_990


def test_no_old_price_in_tiles(offers) -> None:
    """Даже у позиций с пометкой скидки старой цены в плитке нет — поле пусто."""
    assert all(offer.shop_old_price is None for offer in offers)


def test_urls_are_absolute(offers) -> None:
    assert all(offer.url.startswith("https://www.sulpak.kz/") for offer in offers)


def test_discount_page_item_without_vram_suffix() -> None:
    """«RTX3060Ti GAMING OC 8G»: чип прошлого поколения разбирается корректно."""
    offers = sulpak.parse((FIXTURES / "sulpak_discount.html").read_text(encoding="utf-8"))
    assert len(offers) == 1
    offer = offers[0]
    assert offer.chip == "rtx3060ti"
    assert offer.memory_gb == 8


def test_zero_price_is_skipped() -> None:
    """У позиций «под заказ» в data-price стоит 0.0 — их нельзя брать в базу."""
    html = """
    <div id="products">
      <div class="product__item product__item-js" data-name="Видеокарта Gigabyte RTX 5070 WINDFORCE OC 12G (GV-N5070WF3OC-12GD)"
           data-code="111" data-price="0.0" data-brand="Gigabyte"></div>
      <div class="product__item product__item-js" data-name="Видеокарта Gigabyte RTX 5070 AERO OC 12G (GV-N5070AERO OC-12GD)"
           data-code="112" data-price="470990.0" data-brand="Gigabyte"></div>
    </div>
    """
    offers = sulpak.parse(html)
    assert len(offers) == 1
    assert offers[0].price == 470_990


def test_page_2_differs_from_page_1() -> None:
    first = sulpak.parse((FIXTURES / "sulpak_videocards.html").read_text(encoding="utf-8"))
    second = sulpak.parse((FIXTURES / "sulpak_page2.html").read_text(encoding="utf-8"))
    assert {o.identity for o in first}.isdisjoint({o.identity for o in second})
