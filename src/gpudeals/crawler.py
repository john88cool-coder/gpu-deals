"""Цикл обхода: сбор, оценка, одно сообщение на цикл, тревога о поломке."""

from __future__ import annotations

import asyncio
import logging

import httpx

from . import benchmarks
from .config import Settings, settings as default_settings
from .evaluate import Verdict, evaluate, is_new_low
from .models import ItemKind
from .notify import Notifier
from .report import format_breakage, format_digest, format_heartbeat
from .shops import REGISTRY, is_alert_source
from .storage import (
    class_floor_for_cards,
    connect,
    previous_item_count,
    prune_old_observations,
    record_alert,
    record_crawl,
    save_observations,
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
) -> tuple[list[Verdict], list[str], list[tuple[str, int, bool]]]:
    """Обходит магазины, возвращает находки, тревоги о поломке и итоги обхода.

    `watchlist_only` сужает результат до классов из watchlist. Тревога о поломке
    в этом режиме не поднимается: пустой результат может означать просто
    отсутствие нужных моделей в наличии.
    """
    config = config or default_settings
    targets = {name: REGISTRY[name] for name in (shops or REGISTRY)}
    watched = config.watched_class_keys if watchlist_only else None

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

    findings: list[Verdict] = []
    breakages: list[str] = []
    summary: list[tuple[str, int, bool]] = []

    with connect() as conn:
        for shop_name, offers, error in results:
            ok = error is None
            summary.append((shop_name, len(offers), ok))

            if ok and not offers and not watchlist_only:
                previous = previous_item_count(conn, shop_name)
                if previous and previous >= config.thresholds.breakage_min_previous_items:
                    breakages.append(format_breakage(shop_name, previous))

            # В режиме watchlist результат отфильтрован, и его размер нельзя
            # сравнивать с полными обходами — такую запись не ведём.
            if not watchlist_only:
                record_crawl(conn, shop_name, len(offers), ok, error)

        # Эталонные магазины сохраняем первыми: их цены должны попасть в медианы
        # классов и минимумы по картам до того, как оценим остальных.
        for shop_name, offers, _ in results:
            if offers and not is_alert_source(targets[shop_name]):
                save_observations(conn, offers)

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
                verdict = evaluate(conn, offer, config.thresholds, card_floor)
                if verdict.should_alert and is_new_low(conn, offer):
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
    return findings, breakages, summary


def run_once(
    notifier: Notifier,
    shops: list[str] | None = None,
    heartbeat: bool = False,
    watchlist_only: bool = False,
) -> int:
    """Один цикл: собрать, оценить, отправить. Возвращает число находок."""
    findings, breakages, summary = asyncio.run(crawl(shops, watchlist_only=watchlist_only))

    for text in breakages:
        notifier.send(text)

    if findings:
        notifier.send(format_digest(findings))

    if heartbeat:
        notifier.send(format_heartbeat(summary))

    return len(findings)
