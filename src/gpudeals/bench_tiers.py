"""Кураторская таблица относительной производительности и тиров.

PassMark — один синтетический балл. Для объективного ответа «что брать»
нужна вторая шкала — средняя относительная производительность в играх
(1440p, raster) по данным независимых тестов (TechPowerUp / Tom's Hardware
/ GamersNexus). Вместо хрупкого парсинга страниц с защитой таблица хранится
как код: 9 чипов из INTERESTED_CHIPS + ближайшие соседи для контекста.
Обновляется руками раз в квартал — достаточно, новые поколения выходят редко.

Цены для тиров не нужны: уровни строятся по fps, а вывод «лучших» —
через price_per_point поверх этого fps.

Источники-основания (уровни 1440p raster, условные fps-индексы):
TechPowerUp review averages (RTX 5070 Ti / 5070 / 5060 Ti, RX 9070 XT / 9070),
Tom's Hardware GPU hierarchy, ComputerBase/3DCenter roundups.
Индекс нормирован: RTX 5070 Ti = 100.
"""

from __future__ import annotations

from dataclasses import dataclass

# Индекс относит. производительности (1440p raster average), 5070 Ti = 100.
# Округлён до 0.5, по сводным тестам 25-30 игр (TPU), margin ~2-3%.
REL_PERF: dict[str, float] = {
    "rtx5090": 148.0,
    "rtx5080": 125.0,
    "rtx5070ti": 100.0,
    "rtx5070": 87.0,
    "rtx5060ti": 64.0,
    "rtx5060": 55.0,
    "rtx5050": 42.0,
    "rx7900xtx": 112.0,
    "rx7900xt": 103.0,
    "rx7900gre": 92.0,
    "rx9070xt": 95.0,
    "rx9070": 84.0,
    "rx9060xt": 58.0,
    "rx9060": 50.0,
    "rx7800xt": 72.0,
    "rx7600xt": 48.0,
    "rx7600": 42.0,
    "rtx4070tisuper": 98.0,
    "rtx4070ti": 93.0,
    "rtx4070super": 88.0,
    "rtx4070": 78.0,
    "rtx4060ti": 58.0,
    "rtx4060": 50.0,
}

# Тиры — для визуального вывода на дашборде и в отчётах.
# Флагман > Энтузиаст > Верхний средний > Средний > Начальный.
_TIERS: list[tuple[str, float, str]] = [
    ("Флагман", 120, "flagship"),
    ("Энтузиаст", 90, "enthusiast"),
    ("Верхний средний", 70, "upper"),
    ("Средний", 50, "mid"),
    ("Начальный", 0, "entry"),
]


@dataclass(frozen=True)
class ChipBench:
    chip: str
    rel_perf: float
    tier_label: str
    tier_key: str


def chip_bench(chip: str | None) -> ChipBench | None:
    if not chip or chip not in REL_PERF:
        return None
    perf = REL_PERF[chip]
    for label, threshold, key in _TIERS:
        if perf >= threshold:
            return ChipBench(chip=chip, rel_perf=perf, tier_label=label, tier_key=key)
    return None


def price_per_rel(price: int, chip: str | None) -> float | None:
    """Тенге за единицу относит. производительности (низкое — выгоднее)."""
    b = chip_bench(chip)
    if not b or b.rel_perf <= 0:
        return None
    return price / b.rel_perf


def tier_badge(chip: str | None) -> tuple[str, str] | None:
    b = chip_bench(chip)
    if not b:
        return None
    return b.tier_label, b.tier_key
