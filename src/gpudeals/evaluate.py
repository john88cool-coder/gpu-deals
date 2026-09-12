"""Оценка выгодности: три независимых сигнала.

Магазинная скидка сигналом не является — она показывается в тексте справочно.
Причина в данных: у Technodom «-18%» стоит на трети каталога, а позиция с
«-41%» (1 287 990 → 757 990) дороже аналога без скидки за 615 990.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from statistics import median

from .config import Thresholds
from .models import ItemKind, MatchLevel, Offer
from .storage import class_prices, last_alert, last_in_stock, last_price, price_history


class Signal(str, Enum):
    PRICE_DROP = "упало"
    BELOW_CLASS = "дешевле аналогов"
    NEW_IN_BUDGET = "новинка в бюджете"
    TARGET_PRICE = "целевая цена"
    RESTOCK = "в наличие"


@dataclass(frozen=True)
class SignalHit:
    """Структурированные данные сигнала — без готового текста.

    Рендерер собирает из них строки карточки; цифры остаются числами,
    поэтому подписи и глубину истории можно показывать честно.
    """

    signal: Signal
    base: int | None = None   # база сравнения: медиана модели/класса
    sample: int | None = None  # наблюдений или позиций в базе сравнения
    target: int | None = None  # целевая цена (TARGET_PRICE, RESTOCK)
    window_days: int | None = None  # окно тренда (PRICE_DROP)


@dataclass
class Verdict:
    """Итог оценки одного предложения."""

    offer: Offer
    signals: list[SignalHit]
    class_median: int | None = None
    over_budget_by: int | None = None
    perf_vs_class_pct: float | None = None
    build_residual: int | None = None
    # Где та же категория карт стоит дешевле, если дешевле:
    # (магазин, цена, ссылка на оффер).
    cheaper_elsewhere: tuple[str, int, str] | None = None
    # Ни один другой магазин не предлагает этот класс дешевле.
    lowest_in_market: bool = False
    # Глубина истории для «упало»: дней от первого наблюдения в окне.
    drop_span_days: int | None = None

    def hits(self, signal: Signal) -> list[SignalHit]:
        return [hit for hit in self.signals if hit.signal is signal]

    def has(self, signal: Signal) -> bool:
        return any(hit.signal is signal for hit in self.signals)

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
                    first_day = datetime.fromisoformat(history[0][0])
                    span = (datetime.now(UTC) - first_day).days + 1
                    verdict.drop_span_days = span
                    verdict.signals.append(SignalHit(
                        Signal.PRICE_DROP,
                        base=expected,
                        sample=len(history),
                        window_days=thresholds.trend_window_days,
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
                verdict.signals.append(SignalHit(
                    Signal.BELOW_CLASS,
                    base=class_med,
                    sample=len(peers),
                ))

    # Сигнал «новинка в бюджете»: два разных случая. (1) позицию увидели
    # впервые; (2) позиция с историей впервые опустилась под потолок — раньше
    # этот переход (610 000 → 599 000 при потолке 600 000) давал бы ноль
    # сигналов без других оснований.
    if offer.price <= budget:
        last = last_price(conn, offer.identity)
        if last is None and last_alert(conn, offer.identity) is None:
            verdict.signals.append(SignalHit(Signal.NEW_IN_BUDGET))
        elif last is not None and last > budget:
            verdict.signals.append(SignalHit(
                Signal.NEW_IN_BUDGET,
                base=last,
            ))

    # Сигнал «целевая цена»: владелец назвал сумму, при которой берёт эту
    # модель. Медианы и тренды ни при чём — цена дошла до цели, надо брать.
    target = (watch_targets or {}).get(offer.class_key or "")
    if target and offer.price <= target:
        verdict.signals.append(SignalHit(
            Signal.TARGET_PRICE,
            target=target,
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
        verdict.signals.append(SignalHit(
            Signal.RESTOCK,
            target=target,
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
