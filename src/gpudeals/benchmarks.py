"""Справочник производительности GPU: тенге за балл PassMark G3D.

Статический CSV, а не онлайн-парсинг: баллы меняются раз в месяцы, а сетевой
запрос был бы лишней точкой отказа. Обновлять вручную раз в квартал командой
`gpu-deals benchmarks --refresh` (не реализовано намеренно — сверка глазами).

В сообщении показывается только относительная величина: абсолютные ₸ за балл
(порядка 15-20) невозможно прочесть с ходу.
"""

from __future__ import annotations

import csv
from functools import lru_cache

from .config import BENCHMARKS_CSV


@lru_cache(maxsize=1)
def scores() -> dict[str, int]:
    """Баллы G3D по нормализованному ключу чипа."""
    if not BENCHMARKS_CSV.exists():
        return {}
    with BENCHMARKS_CSV.open(encoding="utf-8") as handle:
        return {
            row["chip"]: int(row["passmark_g3d"])
            for row in csv.DictReader(handle)
            if row.get("chip") and row.get("passmark_g3d")
        }


def price_per_point(chip: str | None, price: int) -> float | None:
    """Тенге за балл производительности. Меньше — лучше."""
    if not chip:
        return None
    score = scores().get(chip)
    return price / score if score else None


def relative_value_pct(chip: str | None, price: int, class_median: int | None) -> float | None:
    """Насколько цена за производительность лучше медианы класса, в процентах.

    Внутри одного класса чип одинаков, поэтому отношение баллов сокращается и
    величина сводится к разнице цен. Функция всё равно проходит через баллы:
    когда добавится сравнение между классами, формула не изменится.
    """
    if class_median is None:
        return None
    own = price_per_point(chip, price)
    reference = price_per_point(chip, class_median)
    if not own or not reference:
        return None
    return (reference - own) / reference * 100
