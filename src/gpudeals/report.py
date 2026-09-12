"""Сборка текста уведомления.

Одно сообщение на цикл обхода со всеми находками. Цена за производительность
показывается только в относительном виде — абсолютные ₸ за балл неинтуитивны.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

from . import benchmarks
from .evaluate import Signal, Verdict
from .models import ItemKind, MatchLevel


@dataclass(frozen=True)
class DigestDeal:
    """Лучшее предложение недели: позиция, упавшая сильнее всех."""

    title: str
    shop: str
    price: int
    prev_price: int
    drop_pct: float
    url: str
    observed_day: str  # день, когда зафиксирован минимум (YYYY-MM-DD)


@dataclass(frozen=True)
class DigestValue:
    """Лидер по цене за балл PassMark."""

    title: str
    shop: str
    price: int
    per_point: float


@dataclass(frozen=True)
class MarketDigest:
    """Данные недельного дайджеста рынка, собранные из базы.

    `medians` — (class_key, медиана текущей недели, медиана предыдущей);
    предыдущая может отсутствовать — база ещё не копила две недели.
    `monthly_minima` — (class_key, минимум за 30 дней, магазин, доля
    наблюдений в наличии за тот же месяц).
    """

    medians: list[tuple[str, int | None, int | None]]
    best_deal: DigestDeal | None
    value_leaders: list[DigestValue]
    monthly_minima: list[tuple[str, int, str, float]]


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

# Эмодзи-маркеры сигналов: глаз цепляется за них раньше, чем за текст.
_SIGNAL_EMOJI = {
    Signal.PRICE_DROP: "📉",
    Signal.BELOW_CLASS: "⚡️",
    Signal.NEW_IN_BUDGET: "🆕",
    Signal.TARGET_PRICE: "🎯",
    Signal.RESTOCK: "📦",
}


def _short_title(title: str, max_len: int = 56) -> str:
    """Заголовок для сводa: без «Видеокарта», партномеров и спецификаций.

    Полную версию с партномером владелец увидит на странице товара — в
    сообщении она только съедает место и толкает цену за первый экран.
    """
    cleaned = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title)
    cleaned = re.sub(r"^\s*(видеокарта|компьютер|игровой компьютер)\s+",
                     "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,")
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


def chip_label(class_key: str | None) -> str:
    """«rtx5070ti-16» → «RTX 5070 Ti»: для подписей кнопок и заголовков."""
    if not class_key:
        return ""
    chip = class_key.split("-", 1)[0]
    match = re.match(r"(rtx|rx)(\d{4})(.*)", chip)
    if not match:
        return chip.upper()
    prefix, number, suffix = match.groups()
    for token, pretty in (("super", " Super"), ("gre", " GRE"), ("xtx", " XTX"),
                          ("xt", " XT"), ("ti", " Ti")):
        suffix = suffix.replace(token, pretty)
    return f"{prefix.upper()} {number}{suffix}".strip()


def _signal_headline(signal: Signal) -> str:
    """Первая строка карточки: почему уведомление пришло."""
    return {
        Signal.TARGET_PRICE: "🎯 Цель достигнута",
        Signal.RESTOCK: "📦 В наличие по целевой цене",
        Signal.PRICE_DROP: "📉 Подтверждённое снижение",
        Signal.BELOW_CLASS: "⚡️ Дешевле аналогов",
        Signal.NEW_IN_BUDGET: "🆕 Новинка в бюджете",
    }[signal]


def _signal_line(hit) -> str:
    """Строка сигнала из структурированных данных."""
    if hit.signal is Signal.PRICE_DROP:
        return (f"📉 на {hit.sample} наблюдениях медиана была "
                f"{_money(hit.base)} — сейчас дешевле")
    if hit.signal is Signal.BELOW_CLASS:
        return (f"⚡️ на {_hit_below_pct(hit):.0f}% дешевле медианы класса "
                f"({_money(hit.base)}, {hit.sample} позиций)")
    if hit.signal is Signal.NEW_IN_BUDGET:
        if hit.base is not None:
            return f"🆕 впервые в бюджете: была {_money(hit.base)}"
        return "🆕 новая позиция в бюджете"
    return ""


def _hit_below_pct(hit, price: int) -> float:
    if not hit.base or hit.base <= price:
        return 0.0
    return (hit.base - price) / hit.base * 100


def _chip_with_memory(offer) -> str:
    """«RTX 5070 Ti · 16 ГБ» из класса и объёма."""
    label = chip_label(offer.class_key)
    if offer.memory_gb:
        return f"{label} · {offer.memory_gb} ГБ"
    return label


def format_offer(verdict: Verdict) -> str:
    offer = verdict.offer
    title = _text(_short_title(offer.title))

    # Главная причина — самый весомый сигнал сверху (цель > ресток > упало >
    # дешевле аналогов > новинка). Пустой signals — только over_budget.
    order = [Signal.TARGET_PRICE, Signal.RESTOCK, Signal.PRICE_DROP,
             Signal.BELOW_CLASS, Signal.NEW_IN_BUDGET]
    hits = sorted(verdict.signals,
                  key=lambda h: order.index(h.signal) if h.signal in order else 99)
    main = hits[0] if hits else None

    # Первая строка: что (сигнал), какая группа. Без сигналов — просто название.
    if main:
        lines = [f"{_signal_headline(main.signal)} · {_text(_chip_with_memory(offer))}"]
    else:
        lines = [f"⚠️ {title}"]
    # Вторая: короткое название и магазин.
    lines.append(f"{title} · {_text(offer.shop)}")
    # Третья: цена — главный факт, предупреждение бюджета рядом.
    price_line = f"💰 <b>{_money(offer.price)}</b>"
    if verdict.over_budget_by:
        price_line += f" ⚠️ выше бюджета на {_money(verdict.over_budget_by)}"
    lines.append(price_line)

    # Положение относительно цели — рядом с ценой, это решение владельца.
    for hit in hits:
        if hit.target is not None:
            if offer.price <= hit.target:
                lines.append(f"🎯 на {_money(hit.target - offer.price)} ниже твоей цели")
            else:
                lines.append(f"🎯 до цели {_money(offer.price - hit.target)}")
            break

    # Детали каждого сигнала — с базой сравнения и глубиной выборки.
    for hit in hits:
        if hit.signal is Signal.PRICE_DROP and hit.base is not None:
            span = (f" за доступные {verdict.drop_span_days} дн."
                    if verdict.drop_span_days else "")
            lines.append(
                f"📉 медиана этой модели была {_money(hit.base)}{span} "
                f"({hit.sample} наблюдений)"
            )
        elif hit.signal is Signal.BELOW_CLASS and hit.base is not None:
            pct = _hit_below_pct(hit, offer.price)
            lines.append(
                f"⚡️ на {pct:.0f}% дешевле медианы класса "
                f"({_money(hit.base)}, {hit.sample} позиций)"
            )
        elif hit.signal is Signal.NEW_IN_BUDGET and hit.base is not None:
            lines.append(f"🆕 впервые в бюджете: была {_money(hit.base)}")

    # Рейтинг PassMark — относительный, без абсолютных баллов и места.
    if rating_line := benchmarks.format_rating(offer.class_key, offer.chip):
        lines.append(f"⭐️ {rating_line}")

    # Кросс-магазинное сравнение.
    if verdict.cheaper_elsewhere:
        shop, price, _url = verdict.cheaper_elsewhere
        lines.append(
            f"⚡️ дешевле сейчас: {_text(shop)} — {_money(price)} "
            f"(−{_money(offer.price - price)})"
        )
    elif verdict.lowest_in_market:
        lines.append("🥇 самая низкая цена среди магазинов")

    if verdict.build_residual is not None:
        lines.append(
            f"🧱 остаток за платформу: {_money(verdict.build_residual)} "
            f"(процессор, память, накопитель, плата, корпус, БП)"
        )

    # Магазинная «скидка» — справочно, курсивом.
    if offer.shop_old_price:
        lines.append(
            f"<i>магазин указывает: {_money(offer.shop_old_price)} → "
            f"{_money(offer.price)} (не проверено, справочно)</i>"
        )

    if not offer.in_stock:
        lines.append(f"❌ наличие: {_text(offer.stock_note or 'нет в наличии')}")

    if offer.kind is ItemKind.CARD and offer.match_level is MatchLevel.CLASS:
        lines.append("<i>модель опознана только по классу — сравнение приблизительное</i>")

    # Единственный сигнал «новинка» без других оснований: скидка не подтверждена.
    if hits and hits[0].signal is Signal.NEW_IN_BUDGET and len(hits) == 1:
        lines.append("<i>скидка пока не подтверждена: позиция новая, истории нет</i>")

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


def format_best_deals(
    deals: list[tuple[str, int, str, str, str]],
    medians: dict[str, int],
    targets: dict[str, int],
    best_build: tuple[str, int, str, int] | None = None,
) -> str:
    """Свод «самая выгодная в каждой группе»: одна позиция на класс с контекстом
    для решения — насколько ниже медианы, цена за балл, положение цели."""
    lines = ["<b>🏆 Самые выгодные по группам</b>"]
    for class_key, price, shop, title, _url in deals:
        lines.append(f"<b>{_text(class_key)}</b>: {_money(price)} ({_text(shop)})")
        lines.append(f"   {_text(title)}")
        med = medians.get(class_key)
        if med:
            delta = (med - price) / med * 100
            if delta >= 0.5:
                lines.append(f"   на {delta:.0f}% дешевле медианы класса ({_money(med)})")
        per_point = benchmarks.price_per_point(class_key.split("-")[0], price)
        if per_point:
            lines.append(
                f"   цена за балл: {str(round(per_point, 1)).replace('.', ',')} ₸"
            )
        if (target := targets.get(class_key)) is not None:
            if price <= target:
                lines.append(f"   цель {_money(target)} ✓ достигнута")
            else:
                lines.append(f"   до цели {_money(price - target)}")
    if best_build is not None:
        class_key, price, shop, residual = best_build
        lines.append(
            "🧱 <b>Сборка</b> " + chip_label(class_key) + f": {_money(price)} "
            f"({_text(shop)}) — остаток за платформу {_money(residual)}"
        )
    return "\n".join(lines)


def format_buyers_guide(
    offers: list[tuple[str, int, str]], targets: dict[str, int],
    best_build: tuple[str, int, str, int] | None = None,
) -> str:
    """Ежедневная шпаргалка покупателя: лучшая цена сейчас по каждому классу
    и сколько осталось до личной цели. Если есть сборки — лучшая сборка по
    остатку за платформу."""
    lines = ["<b>── Шпаргалка покупателя ──</b>"]
    for class_key, price, shop in offers:
        line = f"• {class_key}: {_money(price)} ({_text(shop)})"
        if (target := targets.get(class_key)) is not None:
            if price <= target:
                line += f" — цель {_money(target)} ✓"
            else:
                line += f" — до цели {_money(price - target)}"
        lines.append(line)
    if best_build is not None:
        class_key, price, shop, residual = best_build
        lines.append(
            f"• Сборка {class_key}: {_money(price)} ({_text(shop)}) — "
            f"остаток за платформу {_money(residual)}"
        )
    return "\n".join(lines)


def format_heartbeat(results: list[tuple[str, int, bool]]) -> str:
    """Ежедневная строка о живости: сколько магазинов опрошено и позиций найдено."""
    alive = sum(1 for _, _, ok in results if ok)
    total_items = sum(count for _, count, ok in results if ok)
    details = ", ".join(
        f"{_text(shop)}: {count}" if ok else f"{_text(shop)}: ошибка"
        for shop, count, ok in results
    )
    return f"✓ {alive}/{len(results)} магазинов опрошено, {total_items} позиций\n{details}"


def _delta_line(now: int | None, prev: int | None) -> str:
    """Изменение медианы за неделю: «+2%», «−3%» или «—» без прошлой недели."""
    if prev is None or not prev:
        return "—"
    pct = (now - prev) / prev * 100
    if abs(pct) < 0.5:
        return "±0%"
    return f"{pct:+.0f}%"


def format_market_digest(data: MarketDigest) -> str:
    """Недельный дайджест рынка: одно сообщение, читается за минуту.

    Медианы показывают, куда движутся цены по классам (брать сейчас или
    подождать), сделка недели — что упало сильнее всех, лидеры по цене за
    балл — где сейчас максимум производительности за деньги.
    """
    blocks = ["📊 Дайджест рынка за неделю"]

    if data.medians:
        blocks.append("\n<b>── Медианы классов, ₸ ──</b>")
        for class_key, now, prev in data.medians:
            delta = _delta_line(now, prev)
            base = f"• {class_key}: {_money(now) if now else '—'} ({delta})"
            blocks.append(base)
    else:
        blocks.append("\nМедиан пока нет — база только начинает копиться.")

    if data.best_deal:
        deal = data.best_deal
        blocks.append("\n<b>── Самое сильное снижение недели ──</b>")
        blocks.append(
            f"<b>{_text(deal.title)}</b> ({_text(deal.shop)})\n"
            f"{_money(deal.price)}, −{deal.drop_pct:.0f}% за неделю "
            f"(было {_money(deal.prev_price)}, {deal.observed_day})\n"
            f'<a href="{_text(deal.url)}">открыть</a>'
        )
        if deal.price > 600_000:
            blocks.append(
                f"⚠️ выше бюджета на {_money(deal.price - 600_000)}"
            )
    else:
        blocks.append(
            "\nЛучшее предложение недели не определено: нужно две недели истории."
        )

    if data.value_leaders:
        blocks.append("\n<b>── Лидеры по цене за балл ──</b>")
        for index, leader in enumerate(data.value_leaders, start=1):
            blocks.append(
                f"{index}. {_text(leader.title)} ({_text(leader.shop)}) — "
                f"{_money(leader.price)}, "
                f"{str(round(leader.per_point, 1)).replace('.', ',')} ₸/балл"
            )

    if data.monthly_minima:
        blocks.append("\n<b>── Минимумы за месяц наблюдений ──</b>")
        for class_key, price, shop, stock_share in data.monthly_minima:
            share = f"{stock_share * 100:.0f}%"
            blocks.append(
                f"• {class_key}: {_money(price)} ({_text(shop)}) · "
                f"в наличии {share} времени"
            )

    return "\n".join(blocks)
