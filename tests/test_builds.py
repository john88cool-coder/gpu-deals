"""Тесты разбора готовых сборок и валидации объёма видеопамяти."""

from __future__ import annotations

from pathlib import Path

import pytest

from gpudeals.models import ItemKind
from gpudeals.normalize import canonical_vram, extract_chip, extract_memory_gb
from gpudeals.shops import technodom

COMPUTERS = Path(__file__).parent / "fixtures" / "technodom_computers.html"
CARDS_PAGE_2 = Path(__file__).parent / "fixtures" / "technodom_videocards_p2.html"


@pytest.fixture(scope="module")
def mixed_category():
    """Категория «Компьютеры и мониторы» целиком, без фильтрации."""
    return technodom.parse(COMPUTERS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def builds():
    return technodom.parse(COMPUTERS.read_text(encoding="utf-8"), builds_only=True)


def test_builds_only_drops_monitors_and_all_in_ones(mixed_category, builds) -> None:
    assert len(builds) < len(mixed_category)
    assert all(build.kind is ItemKind.BUILD for build in builds)
    assert all(build.chip for build in builds)
    titles = " ".join(build.title.lower() for build in builds)
    assert "монитор" not in titles
    assert "моноблок" not in titles


def test_builds_are_classified_as_builds(builds) -> None:
    neo = next(b for b in builds if "NEO Game A18" in b.title)
    assert neo.kind is ItemKind.BUILD
    assert neo.chip == "rtx5050"
    assert neo.memory_gb == 8  # в названии указано только «DDR5 16GB»
    assert neo.class_key == "rtx5050-8"


def test_ram_and_ssd_are_not_mistaken_for_vram(builds) -> None:
    """«RTX5060/DDR4 16GB/SSD 1TB»: 16 ГБ — оперативная, не видеопамять."""
    a20 = next(b for b in builds if "NEO Game A20" in b.title)
    assert a20.memory_gb == 8


def test_ambiguous_chip_without_vram_gets_no_class(builds) -> None:
    """RTX 5060 Ti выпускается на 8 и 16 ГБ. «RTX 5060 TI/32GB/SSD 1TB» не
    позволяет определить объём, и позиция сознательно остаётся без класса —
    иначе она попала бы в чужое сравнение."""
    a14 = next(b for b in builds if "NEO Game A14" in b.title)
    assert a14.chip == "rtx5060ti"
    assert a14.memory_gb is None
    assert a14.class_key is None


def test_explicit_vram_in_build_is_used(builds) -> None:
    technogaming = next(b for b in builds if "TechnoGaming 287" in b.title)
    assert technogaming.class_key == "rtx5060ti-16"


def test_pagination_metadata(mixed_category) -> None:
    assert technodom.total_pages(COMPUTERS.read_text(encoding="utf-8")) == 9


def test_second_page_of_cards_parses(mixed_category) -> None:
    offers = technodom.parse(CARDS_PAGE_2.read_text(encoding="utf-8"))
    assert len(offers) >= 20
    assert all(offer.kind is ItemKind.CARD for offer in offers)


@pytest.mark.parametrize(
    ("chip", "expected"),
    [
        ("rtx5070", 12),
        ("rtx5070ti", 16),
        ("rtx5060", 8),
        # Две конфигурации — угадывать нельзя.
        ("rtx5060ti", None),
        ("rx9060xt", None),
        ("rtx5090", None),
        (None, None),
    ],
)
def test_canonical_vram(chip: str | None, expected: int | None) -> None:
    assert canonical_vram(chip) == expected


def test_implausible_vram_is_rejected() -> None:
    """RTX 5070 существует только на 12 ГБ: «RTX 5070 / 32 Гб» — не видеопамять."""
    title = "Компьютер (Ci7/RTX 5070/32 Гб/SSD 1TB)"
    chip = extract_chip(title)
    assert chip == "rtx5070"
    assert extract_memory_gb(title, chip) == 12
