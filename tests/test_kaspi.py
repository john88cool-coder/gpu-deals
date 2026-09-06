"""Тесты парсера Kaspi на сохранённых ответах API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpudeals.models import ItemKind
from gpudeals.shops import is_alert_source, kaspi

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cards():
    return kaspi.parse(load("kaspi_rtx5070ti.json"))


@pytest.fixture(scope="module")
def builds():
    return kaspi.parse(load("kaspi_builds.json"))


def test_parses_cards(cards) -> None:
    assert len(cards) >= 10
    assert all(offer.kind is ItemKind.CARD for offer in cards)
    assert all(offer.class_key == "rtx5070ti-16" for offer in cards)


def test_urls_are_absolute(cards) -> None:
    assert all(offer.url.startswith("https://kaspi.kz/") for offer in cards)


def test_old_price_is_never_populated(cards, builds) -> None:
    """Kaspi не публикует старую цену: unitPrice и unitSalePrice всегда равны,
    promo стоит на всех товарах. Заполнять поле нечем, и притворяться нельзя."""
    for offer in [*cards, *builds]:
        assert offer.shop_old_price is None
        assert offer.shop_discount_pct is None


def test_kaspi_is_full_alert_source_now() -> None:
    """С 2026-09-06 Kaspi — источник алертов: сигнал «упало» строится на нашей
    собственной истории снимков, магазинные старые цены ему не нужны."""
    assert is_alert_source(kaspi) is True


def test_builds_are_detected(builds) -> None:
    prebuilt = [offer for offer in builds if offer.kind is ItemKind.BUILD]
    assert prebuilt, "в выборке должны быть готовые сборки"
    it_mr = next((o for o in prebuilt if "IT-MR" in o.title), None)
    if it_mr:
        assert it_mr.class_key == "rtx5070-12"


def test_accessories_without_gpu_are_skipped() -> None:
    """Поиск возвращает и посторонние товары — они не должны попадать в базу."""
    payload = {
        "data": {
            "cards": [
                {"id": "1", "title": "Кабель питания PCIe 8-pin", "unitPrice": 3500},
                {
                    "id": "2",
                    "title": "Palit GeForce RTX 5070 Ti GamingPro 16 Гб",
                    "unitPrice": 645990,
                    "shopLink": "/p/test-1/",
                },
            ]
        }
    }
    offers = kaspi.parse(payload)
    assert len(offers) == 1
    assert offers[0].chip == "rtx5070ti"


def test_identity_includes_shop() -> None:
    """Одна и та же карта у Kaspi и Technodom — два разных предложения."""
    offer = kaspi.parse(load("kaspi_rtx5070ti.json"))[0]
    assert offer.identity.startswith("kaspi:")
