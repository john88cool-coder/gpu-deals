"""Оценка выгодности: три независимых сигнала.

Магазинная скидка сигналом не является — она показывается в тексте справочно.
Причина в данных: у Technodom «-18%» стоит на трети каталога, а позиция с
«-41%» (1 287 990 → 757 990) дороже аналога без скидки за 615 990.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from statistics import median

from .config import Thresholds
from .models import ItemKind, MatchLevel, Offer
from .storage import class_prices, last_alert, price_history


class Signal(str, Enum):
    PRICE_DROP = "упало"
    BELOW_CLASS = "дешевле аналогов"
    NEW_IN_BUDGET = "новинка в бюджете"


@dataclass
class Verdict:
    """Итог оценки одного предложения."""

    offer: Offer
    signals: list[tuple[Signal, str]]
    class_median: int | None = None
    over_budget_by: int | None = None
    perf_vs_class_pct: float | None = None
    build_residual: int | None = None

    @property
    def should_alert(self) -> bool:
        return bool(self.signals)


def _expected_by_trend(history: list[tuple[str, int]]) -> int | None:
    """Ожидаемая цена по окну тренда.

    Медиана окна, а не минимум за всё время: рынок памяти растёт, и абсолютный
    минимум прошлого навсегда заглушил бы сигнал.
    """
    if not history:
        return None
    return int(median(price for _, price in history))


def evaluate(
    conn: sqlite3.Connection,
    offer: Offer,
    thresholds: Thresholds,
    card_price_floor: dict[str, int] | None = None,
) -> Verdict:
    """Оценивает предложение против истории и рынка.

    `card_price_floor` — минимум по классу для отдельных карт; нужен, чтобы
    посчитать остаток за платформу у готовой сборки.
    """
    verdict = Verdict(offer=offer, signals=[])
    budget = (
        thresholds.build_budget if offer.kind is ItemKind.BUILD else thresholds.card_budget
    )

    # Сигнал «упало»: только для точно опознанной модели.
    if offer.match_level is MatchLevel.PART_NUMBER:
        history = price_history(conn, offer.identity, thresholds.trend_window_days)
        if len(history) >= thresholds.min_observations_for_trend:
            expected = _expected_by_trend(history)
            if expected:
                delta_pct = (expected - offer.price) / expected * 100
                if delta_pct >= thresholds.drop_pct:
                    verdict.signals.append((
                        Signal.PRICE_DROP,
                        f"на {delta_pct:.0f}% ниже обычной цены этой модели "
                        f"({expected:,} ₸ за {thresholds.trend_window_days} дн.)".replace(",", " "),
                    ))

    # Сигнал «дешевле аналогов»: медиана по классу, раздельно по типу товара.
    if offer.class_key:
        peers = [p for p in class_prices(conn, offer.kind, offer.class_key) if p != offer.price]
        if len(peers) >= 3:
            class_med = int(median(peers))
            verdict.class_median = class_med
            delta_pct = (class_med - offer.price) / class_med * 100
            if delta_pct >= thresholds.below_class_median_pct:
                approx = (
                    " (сопоставление по классу, не по модели)"
                    if offer.match_level is MatchLevel.CLASS
                    else ""
                )
                verdict.signals.append((
                    Signal.BELOW_CLASS,
                    f"на {delta_pct:.0f}% дешевле медианы класса "
                    f"({class_med:,} ₸){approx}".replace(",", " "),
                ))

    # Сигнал «новинка в бюджете»: позиция впервые оказалась под потолком.
    if offer.price <= budget and last_alert(conn, offer.identity) is None:
        if not price_history(conn, offer.identity, thresholds.trend_window_days):
            verdict.signals.append((
                Signal.NEW_IN_BUDGET,
                f"новая позиция в бюджете (≤ {budget:,} ₸)".replace(",", " "),
            ))

    # Мягкий потолок на карту: выше — не молчим, а помечаем превышение.
    if offer.price > budget:
        verdict.over_budget_by = offer.price - budget

    # Остаток за платформу у сборки: цена сборки минус минимум за такую же карту.
    if offer.kind is ItemKind.BUILD and card_price_floor and offer.class_key:
        floor = card_price_floor.get(offer.class_key)
        if floor:
            verdict.build_residual = offer.price - floor

    return verdict


def is_new_low(conn: sqlite3.Connection, offer: Offer) -> bool:
    """Повтор по позиции допустим только при новом снижении."""
    previous = last_alert(conn, offer.identity)
    return previous is None or offer.price < previous
