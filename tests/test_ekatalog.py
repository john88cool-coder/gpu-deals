"""Тесты парсера e-katalog.kz."""

from __future__ import annotations

from pathlib import Path

import pytest

from gpudeals.models import ItemKind
from gpudeals.shops import ekatalog, is_alert_source

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def offers():
    return ekatalog.parse((FIXTURES / "ekatalog_list.html").read_text(encoding="utf-8"))


def test_parses_catalog(offers) -> None:
    assert len(offers) >= 15
    assert all(offer.kind is ItemKind.CARD for offer in offers)


def test_price_is_lower_bound_of_range(offers) -> None:
    """Показывается диапазон продавцов; рыночный минимум — нижняя граница."""
    asus = next(o for o in offers if "RTX 5060 Dual OC 8GB" in o.title)
    assert asus.price == 225_740


def test_reference_only() -> None:
    """Истории цен у агрегатора нет, поэтому он эталон, а не источник алертов."""
    assert is_alert_source(ekatalog) is False


def test_chip_and_class(offers) -> None:
    msi = next(o for o in offers if "RTX 5070 12G VENTUS" in o.title)
    assert msi.chip == "rtx5070"
    assert msi.class_key == "rtx5070-12"


def test_urls_are_absolute(offers) -> None:
    assert all(offer.url.startswith("https://e-katalog.kz/") for offer in offers)


def test_pagination_metadata() -> None:
    html = (FIXTURES / "ekatalog_list.html").read_text(encoding="utf-8")
    assert ekatalog.total_pages(html) == 10
