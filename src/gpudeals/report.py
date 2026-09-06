"""Сборка текста уведомления.

Одно сообщение на цикл обхода со всеми находками. Цена за производительность
показывается только в относительном виде — абсолютные ₸ за балл неинтуитивны.
"""

from __future__ import annotations

from html import escape

from . import benchmarks
from .evaluate import Signal, Verdict
from .models import ItemKind, MatchLevel


def _money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₸"


def _text(value: str) -> str:
    """Текст магазина внутри HTML-разметки Telegram.

    Названия и ссылки приходят из каталогов и попадают в сообщение с
    `parse_mode: HTML`. Одиночный `&` или `<` в названии — это ответ 400 от Bot
    API, исключение из `run_once` и упавший шаг обхода: база за этот обход не
    коммитится, снимок цен теряется целиком.
    """
    return escape(value, quote=True)


# Ниже этого порога относительная цена за производительность — шум округления,
# и строка о ней только удлиняет сообщение.
_PERF_NOTABLE_PCT = 5.0


def format_offer(verdict: Verdict) -> str:
    offer = verdict.offer
    lines = [f"<b>{_text(offer.title)}</b>", f"Цена: {_money(offer.price)}"]

    for _, explanation in verdict.signals:
        lines.append(f"• {explanation}")

    below_class_shown = any(signal is Signal.BELOW_CLASS for signal, _ in verdict.signals)

    if verdict.class_median and not below_class_shown:
        lines.append(f"Медиана класса: {_money(verdict.class_median)}")

    # Внутри класса чип один и тот же, поэтому цена за производительность
    # повторяет процент из «дешевле медианы» — печатаем только когда тот сигнал
    # не сработал и число само по себе заметное.
    if (
        not below_class_shown
        and verdict.perf_vs_class_pct is not None
        and abs(verdict.perf_vs_class_pct) >= _PERF_NOTABLE_PCT
    ):
        direction = "лучше" if verdict.perf_vs_class_pct > 0 else "хуже"
        lines.append(
            f"Цена за производительность: на {abs(verdict.perf_vs_class_pct):.0f}% "
            f"{direction} медианы класса"
        )

    # Абсолютный рейтинг PassMark: насколько чип сильный вообще и что это
    # значит против текущей карты владельца. Отвечает не на тот же вопрос,
    # что «дешевле медианы класса» (цена против рынка), поэтому строки не
    # дублируют друг друга.
    if rating_line := benchmarks.format_rating(offer.class_key, offer.chip):
        lines.append(rating_line)

    if verdict.build_residual is not None:
        lines.append(
            f"Остаток за платформу: {_money(verdict.build_residual)} "
            f"(процессор, память, накопитель, плата, корпус, БП)"
        )

    if offer.shop_old_price:
        lines.append(
            f"Магазин указывает: {_money(offer.shop_old_price)} → {_money(offer.price)} "
            f"(не проверено, справочно)"
        )

    if verdict.over_budget_by:
        lines.append(f"⚠ Выше бюджета на {_money(verdict.over_budget_by)}")

    if not offer.in_stock or offer.stock_note:
        lines.append(f"Наличие: {_text(offer.stock_note or 'нет в наличии')}")

    # У сборок партномера не бывает по определению, поэтому пометка о
    # приблизительности там ничего не сообщает — только у карт.
    if offer.kind is ItemKind.CARD and offer.match_level is MatchLevel.CLASS:
        lines.append("<i>Модель опознана только по классу — сравнение приблизительное</i>")

    lines.append(f'<a href="{_text(offer.url)}">{_text(offer.shop)}</a>')
    return "\n".join(lines)


def format_digest(verdicts: list[Verdict]) -> str:
    """Одно сообщение на цикл: все находки списком."""
    cards = [v for v in verdicts if v.offer.kind is ItemKind.CARD]
    builds = [v for v in verdicts if v.offer.kind is ItemKind.BUILD]

    blocks: list[str] = [f"🎯 Находок: {len(verdicts)}"]
    if cards:
        blocks.append("\n<b>── Видеокарты ──</b>")
        blocks.extend(format_offer(v) for v in cards)
    if builds:
        blocks.append("\n<b>── Готовые сборки ──</b>")
        blocks.extend(format_offer(v) for v in builds)
    return "\n\n".join(blocks)


def format_breakage(shop: str, previous_count: int) -> str:
    return (
        f"🔴 <b>Парсер сломался</b>\n"
        f"Магазин: {_text(shop)}\n"
        f"Вернул 0 позиций, в прошлый раз было {previous_count}.\n"
        f"Скорее всего изменилась вёрстка или включилась защита."
    )


def format_heartbeat(results: list[tuple[str, int, bool]]) -> str:
    """Ежедневная строка о живости: сколько магазинов опрошено и позиций найдено."""
    alive = sum(1 for _, _, ok in results if ok)
    total_items = sum(count for _, count, ok in results if ok)
    details = ", ".join(
        f"{_text(shop)}: {count}" if ok else f"{_text(shop)}: ошибка"
        for shop, count, ok in results
    )
    return f"✓ {alive}/{len(results)} магазинов опрошено, {total_items} позиций\n{details}"
