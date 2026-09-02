"""Тесты нормализации названий на живых примерах из каталогов."""

from __future__ import annotations

import pytest

from gpudeals.normalize import (
    class_key,
    extract_chip,
    extract_memory_gb,
    extract_part_number,
    looks_like_build,
)


@pytest.mark.parametrize(
    ("title", "chip"),
    [
        ("Видеокарта Asus RTX 5060 DUAL OC 8GB 128bit/G7", "rtx5060"),
        ("Видеокарта Gigabyte RTX 5070 Ti EAGLE OC ICE 16GB", "rtx5070ti"),
        ("Palit GeForce RTX 5060 Ti Infinity 3 16 Гб", "rtx5060ti"),
        ("Видеокарта ASUS Prime Radeon RX 9070 XT OC WHITE 16GB", "rx9070xt"),
        ("MSI GeForce RTX 4070 Ti SUPER Gaming X Slim", "rtx4070tisuper"),
        ("Кабель HDMI 2.1 3 м", None),
    ],
)
def test_extract_chip(title: str, chip: str | None) -> None:
    assert extract_chip(title) == chip


@pytest.mark.parametrize(
    ("title", "memory"),
    [
        ("Видеокарта Asus RTX 5060 DUAL OC 8GB 128bit/G7", 8),
        ("Видеокарта Gigabyte RTX 5070 WINDFORCE OC 12GB 192bit/G7", 12),
        ("Palit GeForce RTX 5060 Ti Infinity 3 16 Гб", 16),
        # У сборки объёмов несколько: берём тот, что стоит сразу после чипа.
        ("IT-MR i5-14400F / RTX 5060 Ti 16 Гб / 32 Гб / SSD 1000 Гб / Win 11", 16),
        ("TD GARANT R5 7500F / RTX 5060 Ti 16 Гб / 32 Гб / SSD 1024 Гб", 16),
    ],
)
def test_extract_memory_gb(title: str, memory: int) -> None:
    chip = extract_chip(title)
    assert extract_memory_gb(title, chip) == memory


@pytest.mark.parametrize(
    ("title", "part_number"),
    [
        (
            "Видеокарта Gigabyte RTX 5070 WINDFORCE OC 12GB [GV-N5070WF3OC-12GD]",
            "GV-N5070WF3OC-12GD",
        ),
        ("Palit RTX 5060 Ti WHITE OC 16GB (NE7506TU19T1-GB2061M)", "NE7506TU19T1-GB2061M"),
        (
            "Видеокарта Asus RTX 5060 DUAL OC 8GB [DUAL-RTX5060-O8G-WHITE ASUS]",
            "DUAL-RTX5060-O8G-WHITE",
        ),
        ("MSI RTX 5060 Ti 16G SHADOW 3X OC 16 Гб", None),
        # INNO3D: группы разной длины в разных сериях.
        (
            "Видеокарта INNO3D RTX 5060 Ti TWIN X2 8GB [N506T2-08D7-193075N]",
            "N506T2-08D7-193075N",
        ),
        (
            "Видеокарта INNO3D RTX 5070 TWIN X2 OC 12Gb [N50702-12D7X-195064N]",
            "N50702-12D7X-195064N",
        ),
        # MSI не публикует партномер отдельной строкой, но Technodom кладёт
        # идентификатор в скобки — этого достаточно для сигнала «упало».
        (
            "Видеокарта MSI RTX 5070 Ti VENTUS 3X OC 16GB 256bit/G7 [G507T-16V3C]",
            "G507T-16V3C",
        ),
        # Фраза из слов без цифр идентификатором не считается.
        ("Видеокарта Palit RTX 5070 [Gaming Pro]", None),
    ],
)
def test_extract_part_number(title: str, part_number: str | None) -> None:
    assert extract_part_number(title) == part_number


@pytest.mark.parametrize(
    ("title", "is_build"),
    [
        ("IT-MR i5-14400F / RTX 5070 12 Гб / 32 Гб / SSD 1000 Гб / Win 11", True),
        ("Системный блок i5-14400F / RTX 5060 Ti 8 Гб / 32 Гб", True),
        ("ITBRO R7 9800X3D / RTX 5070 Ti 16 Гб / 32 Гб / SSD 1000 Гб", True),
        ("Видеокарта Gigabyte RTX 5070 EAGLE OC 12GB 192bit/G7", False),
        ("Palit GeForce RTX 5060 Ti Infinity 3 16 Гб", False),
    ],
)
def test_looks_like_build(title: str, is_build: bool) -> None:
    assert looks_like_build(title) is is_build


def test_class_key_requires_both_parts() -> None:
    assert class_key("rtx5070ti", 16) == "rtx5070ti-16"
    assert class_key("rtx5070ti", None) is None
    assert class_key(None, 16) is None
