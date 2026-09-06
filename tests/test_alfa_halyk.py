"""Тесты парсеров alfa.kz и halykmarket.kz.

Оба сайта открываются только браузером: alfa за анти-ботом Anubis, halyk
отдаёт каталог полному Chromium через channel="chromium" (обычный headless
каталог не загружается — проверено 2026-09-06). Тесты проверяют разбор
сохранённых страниц; сами страницы — реальные фрагменты.
"""

from __future__ import annotations

from pathlib import Path

from gpudeals.models import ItemKind
from gpudeals.shops import alfa, halyk

FIXTURES = Path(__file__).parent / "fixtures"


def test_alfa_converts_mb_memory_to_class() -> None:
    """Память «6144 Mb» в названиях alfa переводится в гигабайты: иначе
    класс «чип + объём» не строится и позиция выпадает из сравнения."""
    offers = alfa.parse((FIXTURES / "alfa_cards.html").read_text(encoding="utf-8"))

    palit = next(o for o in offers if "RTX 3050" in o.title)
    assert palit.class_key == "rtx3050-6"
    assert palit.memory_gb == 6
    assert palit.price == 156_634
    # Название остаётся магазинным, ссылка абсолютной.
    assert "6144 Mb" in palit.title
    assert palit.url.startswith("https://alfa.kz/")


def test_alfa_drops_cards_without_recognised_chip() -> None:
    """В alfa много древних карт (GT 610 и т.п.) — без чипа они не проходят."""
    offers = alfa.parse((FIXTURES / "alfa_cards.html").read_text(encoding="utf-8"))
    assert offers
    assert all(o.chip for o in offers)


def test_halyk_strips_title_prefix_and_resolves_links() -> None:
    offers = halyk.parse((FIXTURES / "halyk_cards.html").read_text(encoding="utf-8"))
    assert len(offers) == 3

    first = offers[0]
    assert not first.title.startswith("На страницу продукта")
    assert first.url.startswith("https://halykmarket.kz/")
    assert first.class_key == "rtx5060-8"
    assert all(o.in_stock for o in offers), "в витрине нет маркеров отсутствия"


def test_halyk_recognises_interested_classes() -> None:
    offers = halyk.parse((FIXTURES / "halyk_cards.html").read_text(encoding="utf-8"))
    classes = {o.class_key for o in offers}
    assert "rtx5070ti-16" in classes
    assert "rtx5060ti-8" in classes
