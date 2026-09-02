"""Тесты парсера forcecom.kz."""

from __future__ import annotations

from pathlib import Path

import pytest

from gpudeals.models import ItemKind
from gpudeals.shops import forcecom

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def offers():
    return forcecom.parse((FIXTURES / "forcecom_cards.html").read_text(encoding="utf-8"))


def test_parses_catalog(offers) -> None:
    assert len(offers) >= 12
    assert all(offer.kind is ItemKind.CARD for offer in offers)


def test_chip_and_memory(offers) -> None:
    palit = next(o for o in offers if "RTX 5060 Ti Infinity 3 OC 16GB" in o.title)
    assert palit.chip == "rtx5060ti"
    assert palit.memory_gb == 16
    assert palit.class_key == "rtx5060ti-16"


def test_part_number_wins_over_spec_brackets(offers) -> None:
    """В названии две группы скобок: спецификация [16 ГБ, GDDR7...] и партномер
    (GV-...). Партномер должен победить — иначе сопоставление между магазинами
    сравнивает спецификации, а не модели."""
    gigabyte = next(o for o in offers if "RTX 5070 Ti WINDFORCE OC" in o.title)
    assert gigabyte.part_number == "GV-N507TWF3OC-16GD"


def test_spec_brackets_are_not_taken_as_part_number(offers) -> None:
    for offer in offers:
        if offer.part_number:
            assert "ГБ" not in offer.part_number
            assert "," not in offer.part_number


def test_old_generation_chips_parse(offers) -> None:
    msi = next(o for o in offers if "RTX3060 VENTUS" in o.title)
    assert msi.chip == "rtx3060"
    assert msi.class_key == "rtx3060-12"


def test_no_old_price_in_listing(offers) -> None:
    """PRICEOLD в листинге forcecom всегда null — поле остаётся пустым."""
    assert all(offer.shop_old_price is None for offer in offers)


def test_pagination_metadata() -> None:
    html = (FIXTURES / "forcecom_cards.html").read_text(encoding="utf-8")
    assert forcecom.total_pages(html) == 14


def test_second_page_differs() -> None:
    first = forcecom.parse((FIXTURES / "forcecom_cards.html").read_text(encoding="utf-8"))
    second = forcecom.parse((FIXTURES / "forcecom_page2.html").read_text(encoding="utf-8"))
    assert second, "на второй странице должны быть позиции"
    assert {o.identity for o in first}.isdisjoint({o.identity for o in second})


def test_urls_are_absolute(offers) -> None:
    assert all(offer.url.startswith("https://forcecom.kz/") for offer in offers)
