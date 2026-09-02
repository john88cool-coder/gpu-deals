"""Тесты парсера Technodom на сохранённой странице каталога."""

from __future__ import annotations

from pathlib import Path

import pytest

from gpudeals.models import ItemKind
from gpudeals.shops import technodom

FIXTURE = Path(__file__).parent / "fixtures" / "technodom_videocards.html"


@pytest.fixture(scope="module")
def offers():
    return technodom.parse(FIXTURE.read_text(encoding="utf-8"))


def test_parses_catalog(offers) -> None:
    assert len(offers) >= 20


def test_all_offers_have_price_and_url(offers) -> None:
    assert all(offer.price > 0 for offer in offers)
    assert all(offer.url.startswith("https://www.technodom.kz/") for offer in offers)


def test_known_product_fields(offers) -> None:
    asus = next(o for o in offers if o.sku == "296115")
    assert asus.price == 239990
    assert asus.shop_old_price == 350990
    assert asus.shop_discount_pct == 32
    assert asus.chip == "rtx5060"
    assert asus.memory_gb == 8
    assert asus.class_key == "rtx5060-8"
    assert asus.part_number == "DUAL-RTX5060-O8G-WHITE"
    assert asus.brand == "Asus"
    assert asus.kind is ItemKind.CARD


def test_chip_recognised_for_most_offers(offers) -> None:
    recognised = [o for o in offers if o.chip and o.memory_gb]
    assert len(recognised) / len(offers) >= 0.9


def test_part_number_found_for_every_offer(offers) -> None:
    """Technodom кладёт идентификатор модели в квадратные скобки для всех позиций,
    поэтому сигнал «упало» работает по всему каталогу, а не только по части."""
    assert all(offer.part_number for offer in offers)


def test_inflated_old_price_is_kept_but_not_a_signal(offers) -> None:
    """Магазин показывает -41% на позиции, которая дороже аналога без скидки.

    Значение сохраняется для показа, но сигналом не является — проверяется
    в тестах оценки.
    """
    inflated = next(o for o in offers if o.shop_old_price and o.shop_old_price > 1_000_000)
    assert inflated.shop_discount_pct is not None
    assert inflated.price < inflated.shop_old_price


def test_old_price_below_current_is_discarded(offers) -> None:
    for offer in offers:
        if offer.shop_old_price is not None:
            assert offer.shop_old_price > offer.price
