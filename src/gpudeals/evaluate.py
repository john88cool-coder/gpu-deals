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
from .storage import class_prices, last_alert, last_in_stock, price_history


class Signal(str, Enum):
    PRICE_DROP = "упало"
    BELOW_CLASS = "дешевле аналогов"
    NEW_IN_BUDGET = "новинка в бюджете"
    TARGET_PRICE = "целевая цена"
    RESTOCK = "в наличие"


@dataclass
class Verdict:
    """Итог оценки одного предложения."""

    offer: Offer
    signals: list[tuple[Signal, str]]
    class_median: int | None = None
    over_budget_by: int | None = None
    perf_vs_class_pct: float | None = None
    build_residual: int | None = None
    # Где та же категория карт стоит дешевле, если дешевле:
    # (магазин, цена, ссылка на оффер).
    cheaper_elsewhere: tuple[str, int, str] | None = None
    # Ни один другой магазин не предлагает этот класс дешевле.
    lowest_in_market: bool = False

    @property
    def should_alert(self) -> bool:
        return bool(self.signals)


def _tenge(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₸"


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
    shop_minima: dict[tuple[ItemKind, str], dict[str, int]] | None = None,
    watch_targets: dict[str, int] | None = None,
) -> Verdict:
    """Оценивает предложение против истории и рынка.

    `card_price_floor` — минимум по классу для отдельных карт; нужен, чтобы
    посчитать остаток за платформу у готовой сборки. `shop_minima` — минимум
    (тип, класс) по каждому магазину из текущего цикла, для строки «Дешевле
    сейчас». `watch_targets` — целевые цены владельца по классам из watchlist.
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
                        f"({_tenge(expected)} за {thresholds.trend_window_days} дн.)",
                    ))

    # Сигнал «дешевле аналогов»: медиана по классу, раздельно по типу товара.
    if offer.class_key:
        peers = class_prices(
            conn, offer.kind, offer.class_key, exclude_identity=offer.identity
        )
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
                    f"({_tenge(class_med)}){approx}",
                ))

    # Сигнал «новинка в бюджете»: позиция впервые оказалась под потолком.
    if offer.price <= budget and last_alert(conn, offer.identity) is None:
        if not price_history(conn, offer.identity, thresholds.trend_window_days):
            verdict.signals.append((
                Signal.NEW_IN_BUDGET,
                f"новая позиция в бюджете (≤ {_tenge(budget)})",
            ))

    # Сигнал «целевая цена»: владелец назвал сумму, при которой берёт эту
    # модель. Медианы и тренды ни при чём — цена дошла до цели, надо брать.
    target = (watch_targets or {}).get(offer.class_key or "")
    if target and offer.price <= target:
        verdict.signals.append((
            Signal.TARGET_PRICE,
            f"цена дошла до цели: {_tenge(offer.price)} (цель {_tenge(target)})",
        ))

    # Сигнал «в наличие»: позиция вернулась на витрину, и цена при возврате не
    # выше личной цели. Прошлые алерты по этой позиции могли быть дешевле —
    # is_new_low подавил бы ресток, поэтому в crawler он обходит дедупликацию.
    if (
        offer.in_stock
        and target
        and offer.price <= target
        and last_in_stock(conn, offer.identity) is False
    ):
        verdict.signals.append((
            Signal.RESTOCK,
            f"появился в наличии по {_tenge(offer.price)} (цель {_tenge(target)})",
        ))

    # Сравнение с другими магазинами: тот же тип товара и класс в текущем
    # цикле — сборки со сборками, карты с картами. Ссылка на более дешёвый
    # оффер идёт в inline-кнопку уведомления.
    if offer.class_key and shop_minima:
        others = {
            shop: price_url
            for shop, price_url in shop_minima.get((offer.kind, offer.class_key), {}).items()
            if shop != offer.shop
        }
        if others:
            best_shop, (best_price, best_url) = min(
                others.items(), key=lambda item: item[1][0]
            )
            if best_price < offer.price:
                verdict.cheaper_elsewhere = (best_shop, best_price, best_url)
            else:
                verdict.lowest_in_market = True

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
