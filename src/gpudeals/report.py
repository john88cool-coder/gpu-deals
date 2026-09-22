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


# Коды магазинов в базе — служебные; в сообщениях — привычные названия.
SHOP_NAMES = {
    "dns": "DNS",
    "alfa": "Alfa",
    "technodom": "Technodom",
    "sulpak": "Sulpak",
    "shop.kz": "Shop.kz",
    "forcecom": "Forcecom",
    "kaspi": "Kaspi",
    "e-katalog": "E-katalog",
    "halyk": "Halyk Market",
}


def shop_label(shop: str) -> str:
    return SHOP_NAMES.get(shop, shop)


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _day(iso_day: str) -> str:
    """«2026-09-17» → «17.09»."""
    try:
        _, month, day = iso_day[:10].split("-")
    except ValueError:
        return iso_day
    return f"{day}.{month}"


def class_label(class_key: str | None) -> str:
    """«rx9060xt-16» → «RX 9060 XT · 16 ГБ»: вместо служебного ключа класса."""
    if not class_key:
        return ""
    label = chip_label(class_key)
    _, _, memory = class_key.partition("-")
    return f"{label} · {memory} ГБ" if memory.isdigit() else label


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
    Signal.BELOW_CLASS: "📊",
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
        Signal.BELOW_CLASS: "📊 Дешевле аналогов",
        Signal.NEW_IN_BUDGET: "🆕 Новинка в бюджете",
    }[signal]


def _signal_line(hit, price: int | None = None) -> str:
    """Строка сигнала из структурированных данных."""
    if hit.signal is Signal.PRICE_DROP:
        return (f"📉 на {hit.sample} наблюдениях медиана была "
                f"{_money(hit.base)} — сейчас дешевле")
    if hit.signal is Signal.BELOW_CLASS:
        pct = _hit_below_pct(hit, price) if price is not None else 0
        return (f"📊 на {pct:.0f}% дешевле медианы класса "
                f"({_money(hit.base)}, {hit.sample} "
                f"{_plural(hit.sample, 'позиция', 'позиции', 'позиций')})")
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
    """Карточка находки: зачем пришла, что и где, цена, основания, контекст.

    Название — ссылка на товар: при десятке находок сопоставлять текст с
    кнопками под сообщением неудобно.
    """
    offer = verdict.offer
    title = _text(_short_title(offer.title))
    link = f'<a href="{_text(offer.url)}">{title}</a>' if offer.url else title

    # Главная причина — самый весомый сигнал сверху (цель > ресток > упало >
    # дешевле аналогов > новинка). Пустой signals — только over_budget.
    order = [Signal.TARGET_PRICE, Signal.RESTOCK, Signal.PRICE_DROP,
             Signal.BELOW_CLASS, Signal.NEW_IN_BUDGET]
    hits = sorted(verdict.signals,
                  key=lambda h: order.index(h.signal) if h.signal in order else 99)
    main = hits[0] if hits else None

    # Первая строка: почему пришло и какой класс.
    if main:
        lines = [f"<b>{_signal_headline(main.signal)}</b> · {_text(_chip_with_memory(offer))}"]
    else:
        lines = [f"⚠️ <b>{_text(_chip_with_memory(offer)) or 'Позиция'}</b>"]
    # Вторая: название-ссылка и магазин.
    lines.append(f"{link} · {_text(shop_label(offer.shop))}")
    # Третья: цена — главный факт, предупреждение бюджета рядом.
    price_line = f"💰 <b>{_money(offer.price)}</b>"
    if verdict.over_budget_by:
        price_line += f" · ⚠️ выше бюджета на {_money(verdict.over_budget_by)}"
    lines.append(price_line)

    # Положение относительно цели — это решение владельца, сразу под ценой.
    for hit in hits:
        if hit.target is not None:
            if offer.price <= hit.target:
                lines.append(f"✅ ниже твоей цели на {_money(hit.target - offer.price)}")
            else:
                lines.append(f"🎯 дороже цели на {_money(offer.price - hit.target)}")
            break

    # Основания сигналов — с базой сравнения и глубиной выборки.
    for hit in hits:
        if hit.signal is Signal.PRICE_DROP and hit.base is not None:
            span = (f" за {verdict.drop_span_days} дн."
                    if verdict.drop_span_days else "")
            sample = hit.sample or 0
            lines.append(
                f"📉 медиана модели{span} — {_money(hit.base)} "
                f"({sample} {_plural(sample, 'наблюдение', 'наблюдения', 'наблюдений')})"
            )
        elif hit.signal is Signal.BELOW_CLASS and hit.base is not None:
            lines.append(_signal_line(hit, offer.price))
        elif hit.signal is Signal.NEW_IN_BUDGET and hit.base is not None:
            lines.append(f"🆕 впервые в бюджете, было {_money(hit.base)}")

    # Где брать: дешевле в другом магазине или здесь минимум рынка.
    if verdict.cheaper_elsewhere:
        shop, price, _url = verdict.cheaper_elsewhere
        lines.append(
            f"↘️ дешевле в {_text(shop_label(shop))}: {_money(price)} "
            f"(−{_money(offer.price - price)})"
        )
    elif verdict.lowest_in_market:
        lines.append("🥇 самая низкая цена среди магазинов")

    if verdict.build_residual is not None:
        lines.append(
            f"🧱 за платформу без карты: {_money(verdict.build_residual)}"
        )

    # Насколько чип сильный вообще — одна строка.
    if rating_line := benchmarks.format_rating(offer.class_key, offer.chip):
        lines.append(f"⭐️ {rating_line}")

    if not offer.in_stock:
        lines.append(f"❌ {_text(offer.stock_note or 'нет в наличии')}")

    # Оговорки — одной курсивной строкой, а не тремя.
    notes: list[str] = []
    if offer.shop_old_price:
        notes.append(f"зачёркнутая цена магазина {_money(offer.shop_old_price)} — не проверена")
    if offer.kind is ItemKind.CARD and offer.match_level is MatchLevel.CLASS:
        notes.append("модель опознана по классу, сравнение приблизительное")
    if hits and hits[0].signal is Signal.NEW_IN_BUDGET and len(hits) == 1:
        notes.append("позиция новая, истории цены ещё нет")
    if notes:
        lines.append("<i>" + "; ".join(notes) + "</i>")

    return "\n".join(lines)


def format_digest(verdicts: list[Verdict]) -> str:
    """Одно сообщение на цикл: все находки списком."""
    cards = [v for v in verdicts if v.offer.kind is ItemKind.CARD]
    builds = [v for v in verdicts if v.offer.kind is ItemKind.BUILD]

    count = len(verdicts)
    blocks: list[str] = [
        f"🔔 <b>{count} {_plural(count, 'находка', 'находки', 'находок')}</b>"
    ]
    if cards:
        blocks.append("<b>── Видеокарты ──</b>")
        blocks.extend(format_offer(v) for v in cards)
    if builds:
        blocks.append("<b>── Готовые сборки ──</b>")
        blocks.extend(format_offer(v) for v in builds)
    return "\n\n".join(blocks)


def format_breakage(shop: str, previous_count: int) -> str:
    return (
        f"🔴 <b>Парсер сломался: {_text(shop_label(shop))}</b>\n"
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
    для решения — насколько ниже медианы, цена за балл, положение цели.

    Каждая группа визуально отделена: чип с памятью жирным, цена жирным,
    название подрезано, медиана/балл/цель — отдельными строками."""
    lines = ["🏆 <b>Лучшая цена сейчас по каждой модели</b>"]
    for class_key, price, shop, title, _url in deals:
        target = targets.get(class_key)
        mark = "✅" if target is not None and price <= target else "▫️"
        lines.append(
            f"\n{mark} <b>{_text(class_label(class_key))}</b> — <b>{_money(price)}</b>"
            f" · {_text(shop_label(shop))}"
        )
        lines.append(_text(_short_title(title, max_len=64)))
        facts: list[str] = []
        med = medians.get(class_key)
        if med:
            delta = (med - price) / med * 100
            if delta >= 0.5:
                facts.append(f"−{delta:.0f}% к медиане")
        per_point = benchmarks.price_per_point(class_key.split("-")[0], price)
        if per_point:
            facts.append(f"{str(round(per_point, 1)).replace('.', ',')} ₸/балл")
        if target is not None:
            if price <= target:
                facts.append(f"цель {_money(target)} достигнута")
            else:
                facts.append(f"дороже цели на {_money(price - target)}")
        if facts:
            lines.append(" · ".join(facts))
    if best_build is not None:
        class_key, price, shop, residual = best_build
        lines.append(
            f"\n🧱 <b>Сборка с {_text(chip_label(class_key))}</b> — <b>{_money(price)}</b>"
            f" · {_text(shop_label(shop))}\nза платформу без карты: {_money(residual)}"
        )
    lines.append(
        "\n<i>₸/балл — цена за единицу производительности PassMark, меньше — выгоднее</i>"
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
        line = f"• {class_label(class_key)}: {_money(price)} ({_text(shop_label(shop))})"
        if (target := targets.get(class_key)) is not None:
            if price <= target:
                line += f" — цель {_money(target)} ✓"
            else:
                line += f" — до цели {_money(price - target)}"
        lines.append(line)
    if best_build is not None:
        class_key, price, shop, residual = best_build
        lines.append(
            f"• Сборка с {chip_label(class_key)}: {_money(price)} "
            f"({_text(shop_label(shop))}) — "
            f"остаток за платформу {_money(residual)}"
        )
    return "\n".join(lines)


def format_heartbeat(results: list[tuple[str, int, bool]]) -> str:
    """Ежедневная строка о живости: сколько магазинов опрошено и позиций найдено."""
    alive = sum(1 for _, _, ok in results if ok)
    total_items = sum(count for _, count, ok in results if ok)
    details = ", ".join(
        f"{_text(shop_label(shop))}: {count}" if ok
        else f"{_text(shop_label(shop))}: ⚠️ ошибка"
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


def format_market_digest(data: MarketDigest, card_budget: int = 600_000) -> str:
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
            base = f"• {class_label(class_key)}: {_money(now) if now else '—'} ({delta})"
            blocks.append(base)
    else:
        blocks.append("\nМедиан пока нет — база только начинает копиться.")

    if data.best_deal:
        deal = data.best_deal
        blocks.append("\n<b>── Самое сильное снижение недели ──</b>")
        blocks.append(
            f'<a href="{_text(deal.url)}">{_text(_short_title(deal.title))}</a>'
            f" · {_text(shop_label(deal.shop))}\n"
            f"<b>{_money(deal.price)}</b>, −{deal.drop_pct:.0f}% за неделю "
            f"(было {_money(deal.prev_price)}, минимум {_day(deal.observed_day)})"
        )
        if deal.price > card_budget:
            blocks.append(
                f"⚠️ выше бюджета на {_money(deal.price - card_budget)}"
            )
    else:
        blocks.append(
            "\nЛучшее предложение недели не определено: нужно две недели истории."
        )

    if data.value_leaders:
        blocks.append("\n<b>── Лидеры по цене за балл (меньше — выгоднее) ──</b>")
        for index, leader in enumerate(data.value_leaders, start=1):
            blocks.append(
                f"{index}. {_text(_short_title(leader.title))} "
                f"({_text(shop_label(leader.shop))}) — "
                f"{_money(leader.price)}, "
                f"{str(round(leader.per_point, 1)).replace('.', ',')} ₸/балл"
            )

    if data.monthly_minima:
        blocks.append("\n<b>── Минимумы за месяц наблюдений ──</b>")
        for class_key, price, shop, stock_share in data.monthly_minima:
            share = f"{stock_share * 100:.0f}%"
            blocks.append(
                f"• {class_label(class_key)}: {_money(price)} ({_text(shop_label(shop))}) · "
                f"в наличии {share} времени"
            )

    return "\n".join(blocks)
