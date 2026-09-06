"""Цикл обхода: сбор, оценка, одно сообщение на цикл, тревога о поломке."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from statistics import median

import httpx

from . import benchmarks
from .config import INTERESTED_CHIPS, Settings, settings as default_settings
from .evaluate import Signal, Verdict, evaluate, is_new_low
from .models import ItemKind
from .notify import Notifier
from .report import (
    DigestDeal,
    DigestValue,
    MarketDigest,
    format_breakage,
    format_buyers_guide,
    format_digest,
    format_heartbeat,
    format_market_digest,
)
from .shops import REGISTRY, is_alert_source
from .storage import (
    class_best_offers,
    class_floor_for_cards,
    compact,
    connect,
    previous_item_count,
    prune_old_observations,
    record_alert,
    record_crawl,
    save_observations,
    shop_summary,
)

log = logging.getLogger("gpudeals")


async def _fetch_shop(client: httpx.AsyncClient, shop_name: str, module) -> tuple[str, list, str | None]:
    try:
        offers = await module.fetch(client)
        return shop_name, offers, None
    except Exception as exc:  # noqa: BLE001 — один упавший магазин не должен ронять обход
        log.warning("магазин %s: %s", shop_name, exc)
        return shop_name, [], str(exc)


async def crawl(
    shops: list[str] | None = None,
    config: Settings | None = None,
    watchlist_only: bool = False,
    stale_hours: float | None = None,
) -> tuple[list[Verdict], list[str], list[tuple[str, int, bool]]]:
    """Обходит магазины, возвращает находки, тревоги о поломке и итоги обхода.

    `watchlist_only` сужает результат до классов из watchlist. Тревога о поломке
    в этом режиме не поднимается: пустой результат может означать просто
    отсутствие нужных моделей в наличии. `stale_hours` — режим catch-up для
    Actions: обходить только если последний обход старше N часов, иначе тихо
    выйти (GitHub задерживает и отбрасывает запуски по расписанию).
    """
    config = config or default_settings
    targets = {name: REGISTRY[name] for name in (shops or REGISTRY)}
    watched = config.watched_class_keys if watchlist_only else None

    if stale_hours is not None:
        with connect() as conn:
            row = conn.execute("SELECT MAX(observed_at) AS at FROM observations").fetchone()
        if row and row["at"]:
            last = datetime.fromisoformat(row["at"])
            age = (datetime.now(UTC) - last).total_seconds() / 3600
            if age < stale_hours:
                log.info(
                    "последний обход %.1f ч назад (< %.1f ч) — обход не требуется",
                    age, stale_hours,
                )
                return [], [], []

    headers = {"User-Agent": config.user_agent, "Accept-Language": "ru,en;q=0.8"}
    async with httpx.AsyncClient(
        headers=headers, timeout=config.request_timeout, follow_redirects=True
    ) as client:
        results = await asyncio.gather(
            *(_fetch_shop(client, name, module) for name, module in targets.items())
        )

    if watched is not None:
        results = [
            (shop, [o for o in offers if o.class_key in watched], error)
            for shop, offers, error in results
        ]

    # Сколько позиций парсер отдал на самом деле — до наших фильтров. Таблица
    # crawls измеряет здоровье парсера: тревога «вернул 0» не должна срабатывать
    # там, где магазин жив, но весь его ассортимент отфильтрован по объёму.
    raw_counts = {shop: len(offers) for shop, offers, _ in results}

    # Решение владельца: интересуют только новейшие серии (INTERESTED_CHIPS)
    # с памятью больше 8 ГБ. Позиции интересных чипов с неопознанным объёмом
    # остаются — за «неизвестно» может прятаться 16 ГБ. Всё прочее (5080/5090,
    # старые серии, рабочие карты) не собирается вовсе.
    skip = config.thresholds.skip_memory_gb
    results = [
        (
            shop,
            [
                o for o in offers
                if o.chip in INTERESTED_CHIPS
                and (o.memory_gb is None or o.memory_gb > skip)
            ],
            error,
        )
        for shop, offers, error in results
    ]

    findings: list[Verdict] = []
    breakages: list[str] = []
    summary: list[tuple[str, int, bool]] = []

    with connect() as conn:
        for shop_name, offers, error in results:
            ok = error is None
            summary.append((shop_name, raw_counts[shop_name], ok))

            if ok and not offers and not watchlist_only:
                previous = previous_item_count(conn, shop_name)
                if previous and previous >= config.thresholds.breakage_min_previous_items:
                    breakages.append(format_breakage(shop_name, previous))

            # В режиме watchlist результат отфильтрован, и его размер нельзя
            # сравнивать с полными обходами — такую запись не ведём.
            if not watchlist_only:
                record_crawl(conn, shop_name, raw_counts[shop_name], ok, error)

        # Эталонные магазины сохраняем первыми: их цены должны попасть в медианы
        # классов и минимумы по картам до того, как оценим остальных.
        for shop_name, offers, _ in results:
            if offers and not is_alert_source(targets[shop_name]):
                save_observations(conn, offers)

        # Минимумы (тип товара, класс) по магазинам из текущего цикла — база
        # строки «Дешевле сейчас». Только свежие офферы, без базы: цена
        # трёхдневной давности отвечает на вопрос «где было дешевле», а не
        # «где дешевле сейчас». Сборки сравниваются со сборками, карты —
        # с картами: сборка всегда «дешевле» голой карты, и без раздельного
        # учёта строка бессмысленна. Магазины цикла ещё не сохранены,
        # поэтому минимум собираем в памяти.
        shop_minima: dict[tuple[ItemKind, str], dict[str, tuple[int, str]]] = {}
        for _, offers, _ in results:
            for offer in offers:
                if offer.class_key and offer.in_stock:
                    per_shop = shop_minima.setdefault((offer.kind, offer.class_key), {})
                    known = per_shop.get(offer.shop)
                    if known is None or offer.price < known[0]:
                        per_shop[offer.shop] = (offer.price, offer.url)

        # Целевые цены владельца по классам watchlist: цена дошла до цели —
        # алерт независимо от медиан и трендов.
        watch_targets = {
            model.class_key: model.target_price
            for model in config.watchlist
            if model.target_price is not None
        }

        for shop_name, offers, _ in results:
            if not offers or not is_alert_source(targets[shop_name]):
                continue

            # Минимум по классу среди отдельных карт — база для остатка у сборок.
            # Берём по всем магазинам из базы, а не только из текущего обхода:
            # сборка Technodom должна сравниваться с рыночным минимумом на карту.
            card_floor = class_floor_for_cards(conn)
            for offer in offers:
                if offer.kind is ItemKind.CARD and offer.class_key:
                    current = card_floor.get(offer.class_key)
                    if current is None or offer.price < current:
                        card_floor[offer.class_key] = offer.price

            shop_findings: list[Verdict] = []
            for offer in offers:
                # Отсутствующие товары пишутся в базу, но не будят: алерт «на
                # самом деле этого товара нет» — не находка.
                if not offer.in_stock:
                    continue
                verdict = evaluate(
                    conn, offer, config.thresholds, card_floor, shop_minima, watch_targets
                )
                # Ресток по целевой цене обходит is_new_low: прошлый алерт мог
                # быть дешевле, но «вернулся в наличие по цели» — само по себе
                # новость, которую владелец просил не терять.
                restocked = any(s is Signal.RESTOCK for s, _ in verdict.signals)
                if verdict.should_alert and (is_new_low(conn, offer) or restocked):
                    verdict.perf_vs_class_pct = benchmarks.relative_value_pct(
                        offer.chip, offer.price, verdict.class_median
                    )
                    shop_findings.append(verdict)

            # Наблюдения сохраняем после оценки, иначе текущая цена попала бы
            # в собственную историю и размыла сигнал «упало».
            save_observations(conn, offers)

            for verdict in shop_findings:
                record_alert(conn, verdict.offer.identity, verdict.offer.price)
            findings.extend(shop_findings)

        # Удержание размера: глубже 30 дней данные не нужны ни одному сигналу.
        pruned = prune_old_observations(conn)

    if pruned:
        log.info("удалено устаревших наблюдений: %s", pruned)
        # VACUUM только после реального удаления: он переписывает файл целиком,
        # а вне транзакции его можно выполнить лишь на закрытом соединении.
        if freed := compact():
            log.info("база сжата, освобождено байт: %s", freed)
    return findings, breakages, summary


def _alert_buttons(findings: list[Verdict]) -> list[list[tuple[str, str]]] | None:
    """Inline-кнопки для дайджеста находок: по строке на каждую находку.

    Текст кнопки ограничен 64 символами Telegram, поэтому магазин вместо
    заголовка: класс и так написан в сообщении над ссылкой.
    """
    rows: list[list[tuple[str, str]]] = []
    for verdict in findings:
        offer = verdict.offer
        row = [(f"Открыть в {offer.shop}", offer.url)]
        if verdict.cheaper_elsewhere:
            shop, _price, url = verdict.cheaper_elsewhere
            row.append((f"Где дешевле: {shop}", url))
        rows.append(row)
    return rows or None


def run_once(
    notifier: Notifier,
    shops: list[str] | None = None,
    watchlist_only: bool = False,
    stale_hours: float | None = None,
) -> int:
    """Один цикл: собрать, оценить, отправить. Возвращает число находок."""
    findings, breakages, _ = asyncio.run(
        crawl(shops, watchlist_only=watchlist_only, stale_hours=stale_hours)
    )

    for text in breakages:
        notifier.send(text)

    if findings:
        notifier.send(format_digest(findings), buttons=_alert_buttons(findings))

    return len(findings)


def send_heartbeat(notifier: Notifier, shops: list[str] | None = None) -> None:
    """Строка о живости + ежедневная шпаргалка покупателя, без нового обхода.

    Раньше сюда шёл полный обход всех семи магазинов с браузерным DNS, а его
    находки отправлялись и помечались как отправленные — но workflow heartbeat
    базу не коммитит, поэтому пометка терялась и та же находка приходила
    повторно со следующим обходом.
    """
    with connect() as conn:
        summary = shop_summary(conn, shops or list(REGISTRY))
        best = class_best_offers(
            conn,
            INTERESTED_CHIPS,
            default_settings.thresholds.skip_memory_gb,
        )
    targets = {
        model.class_key: model.target_price
        for model in default_settings.watchlist
        if model.target_price is not None
    }
    text = format_heartbeat(summary)
    if best:
        # Порядок шпаргалки — порядок watchlist: интересы владельца сверху.
        order = {ck: i for i, ck in enumerate(default_settings.watched_class_keys)}
        offers = sorted(best.items(), key=lambda item: order.get(item[0], 99))
        text += "\n\n" + format_buyers_guide(
            [(ck, price, shop) for ck, (shop, price) in offers], targets
        )
    notifier.send(text)


# Классов в базе больше двух десятков; в дайджесте оставляем самые подвижные,
# иначе сообщение превращается в таблицу.
_DIGEST_TOP_MOVERS = 10
_DIGEST_TOP_VALUE = 3


def _class_weekly_medians(
    conn, since: str, until: str | None = None
) -> dict[str, int]:
    """Медиана класса за окно: по минимуму каждой позиции, только в наличии.

    Минимум на позицию, а не все наблюдения: иначе часто опрашиваемые классы
    (watchlist) перетягивали бы медиану к своим частым точкам. Фильтры
    интересов владельца соблюдены и здесь: до-фильтровые строки старых обходов
    (5080, 30-я серия, ≤8 ГБ) не должны попадать в дайджест.
    """
    skip = default_settings.thresholds.skip_memory_gb
    chips_q = ",".join("?" * len(INTERESTED_CHIPS))
    query = f"""SELECT class_key, MIN(price) AS price FROM observations
                WHERE kind = 'card' AND class_key IS NOT NULL AND in_stock = 1
                      AND chip IN ({chips_q})
                      AND (memory_gb IS NULL OR memory_gb > ?)
                      AND observed_at >= ?"""
    params: list = sorted(INTERESTED_CHIPS) + [skip, since]
    if until is not None:
        query += " AND observed_at < ?"
        params.append(until)
    query += " GROUP BY identity"

    per_class: dict[str, list[int]] = {}
    for row in conn.execute(query, params):
        per_class.setdefault(row["class_key"], []).append(row["price"])
    return {
        class_key: int(median(prices))
        for class_key, prices in per_class.items()
        if len(prices) >= 3
    }


def _market_digest(conn) -> MarketDigest:
    """Собирает данные недельного дайджеста из локальной базы.

    Обхода здесь нет и быть не должно: дайджест читает то, что уже накопили
    обходы, — как heartbeat.
    """
    now = datetime.now(UTC)
    cut7 = (now - timedelta(days=7)).isoformat(timespec="seconds")
    cut14 = (now - timedelta(days=14)).isoformat(timespec="seconds")

    medians_now = _class_weekly_medians(conn, cut7)
    medians_prev = _class_weekly_medians(conn, cut14, until=cut7)

    watched = default_settings.watched_class_keys

    def _sort_key(item: tuple[str, int | None, int | None]):
        class_key, now_m, prev_m = item
        watched_first = 0 if class_key in watched else 1
        if now_m and prev_m:
            movement = abs(now_m - prev_m) / prev_m
        else:
            movement = -1.0  # без пары недель класс встаёт в конец списка
        return (watched_first, -movement)

    medians = sorted(
        ((ck, medians_now.get(ck), medians_prev.get(ck)) for ck in medians_now),
        key=_sort_key,
    )[:_DIGEST_TOP_MOVERS]

    # Сделка недели: позиция, чья минимальная цена за эту неделю просела сильнее
    # всего против минимума прошлой. MIN(price) с «голыми» колонками —
    # документированное поведение SQLite: shop/title/url берутся из строки с
    # минимумом. Только в наличии: «предложение» должно быть покупаемым.
    # Фильтры интересов — как во всём дайджесте: интересуют только новейшие
    # серии и память больше 8 ГБ, до-фильтровые строки старых обходов мимо.
    skip = default_settings.thresholds.skip_memory_gb
    chips_q = ",".join("?" * len(INTERESTED_CHIPS))
    interest_filter = f"chip IN ({chips_q}) AND (memory_gb IS NULL OR memory_gb > ?)"
    interest_params = sorted(INTERESTED_CHIPS) + [skip]
    prev_min = {
        row["identity"]: row["price"]
        for row in conn.execute(
            f"""SELECT identity, MIN(price) AS price FROM observations
                WHERE kind = 'card' AND in_stock = 1 AND {interest_filter}
                  AND observed_at >= ? AND observed_at < ?
                GROUP BY identity""",
            (*interest_params, cut14, cut7),
        )
    }
    best_deal: DigestDeal | None = None
    for row in conn.execute(
        f"""SELECT identity, MIN(price) AS price, shop, title, url
            FROM observations
            WHERE kind = 'card' AND in_stock = 1 AND {interest_filter}
                  AND observed_at >= ?
            GROUP BY identity""",
        (*interest_params, cut7),
    ):
        prev = prev_min.get(row["identity"])
        if not prev or prev <= row["price"]:
            continue
        drop = (prev - row["price"]) / prev * 100
        if best_deal is None or drop > best_deal.drop_pct:
            best_deal = DigestDeal(
                title=row["title"], shop=row["shop"], price=row["price"],
                prev_price=prev, drop_pct=drop, url=row["url"],
            )

    # Лидеры по цене за балл: последняя наблюдённая цена позиции за неделю,
    # делённая на балл PassMark её чипа. MAX(observed_at) с «голыми» колонками
    # даёт именно последнюю строку позиции.
    latest: dict[str, dict] = {}
    for row in conn.execute(
        f"""SELECT identity, MAX(observed_at) AS at, price, shop, title, url,
                   chip, class_key
            FROM observations
            WHERE kind = 'card' AND in_stock = 1 AND {interest_filter}
                  AND observed_at >= ?
            GROUP BY identity""",
        (*interest_params, cut7),
    ):
        latest[row["identity"]] = dict(row)

    candidates: list[DigestValue] = []
    for row in latest.values():
        rating = benchmarks.rating_for(row["class_key"], row["chip"])
        if rating and rating.g3d > 0:
            title = row["title"]
            # Заголовки магазинов длинные: в дайджесте их подрезаем, полный
            # титул всегда есть в алертах и по ссылке.
            if len(title) > 70:
                title = title[:69].rstrip() + "…"
            candidates.append(
                DigestValue(
                    title=title, shop=row["shop"], price=row["price"],
                    per_point=row["price"] / rating.g3d,
                )
            )
    value_leaders = sorted(candidates, key=lambda v: v.per_point)[:_DIGEST_TOP_VALUE]

    # Минимумы за месяц наблюдений: ориентир «ниже пока не бывало». Окно —
    # весь удерживаемый практикой месяц; та же фильтрация интересов.
    cut30 = (now - timedelta(days=30)).isoformat(timespec="seconds")
    monthly_minima = [
        (row["class_key"], row["price"], row["shop"])
        for row in conn.execute(
            f"""SELECT class_key, MIN(price) AS price, shop
                FROM observations
                WHERE kind = 'card' AND in_stock = 1 AND {interest_filter}
                      AND class_key IS NOT NULL AND observed_at >= ?
                GROUP BY class_key""",
            (*interest_params, cut30),
        )
    ]

    return MarketDigest(
        medians=medians, best_deal=best_deal, value_leaders=value_leaders,
        monthly_minima=monthly_minima,
    )


def send_digest(notifier: Notifier) -> None:
    """Недельный дайджест рынка: медианы в динамике, сделка недели, лидеры
    по цене за балл. Читает локальную базу, обхода не делает."""
    with connect() as conn:
        data = _market_digest(conn)
    notifier.send(format_market_digest(data))
