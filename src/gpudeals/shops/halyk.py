"""Парсер halykmarket.kz — маркетплейс Halyk Bank.

Сайт — Nuxt-приложение, которое распознаёт headless-браузер: в обычном
headless Chromium каталог не загружается вовсе (в DOM нет ни одного товара,
XHR за данными не уходит; проверено 2026-09-06). Лечится запуском полного
Chromium через `channel="chromium"` — тот же headless, но полный движок,
который сайт считает настоящим браузером.

Разметка после рендера: карточки — сами ссылки `a.h-product-card` с полным
названием в `title` (с приставкой «На страницу продукта») и относительным
href; цена — в `.h-product-card__price`. Подгрузка — бесконечным скроллом:
фETCH прокручивает страницу, пока появляются новые карточки.

robots.txt запрещает URL с `?sort=` и `?f=`, поэтому берём чистую категорию
`/category/videokarti` без параметров — фильтры интересов владельца применяет
crawler на уровне коллекции.
"""

from __future__ import annotations

import asyncio
import logging

from selectolax.parser import HTMLParser

from ..models import ItemKind, Offer
from ..normalize import (
    class_key,
    extract_brand,
    extract_chip,
    extract_memory_gb,
    extract_part_number,
    looks_like_build,
)
from .paging import new_offers

SHOP = "halyk"
BASE = "https://halykmarket.kz"
CATALOG_URL = f"{BASE}/category/videokarti"

_PAGE_TIMEOUT_MS = 90_000
# Раундов прокрутки: каталог подгружается бесконечным скроллом.
_MAX_SCROLL_ROUNDS = 10
_SCROLL_DELAY_S = 2.5
# Служебный UA HeadlessChrome сайт считает ботом и каталог не отдаёт.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

log = logging.getLogger(__name__)

_TITLE_PREFIX = "На страницу продукта "
_OUT_MARKERS = ("нет в наличии", "под заказ", "ожидается")


def parse(html: str) -> list[Offer]:
    """Разбирает отрендеренную страницу каталога halykmarket."""
    tree = HTMLParser(html)
    offers: list[Offer] = []
    for card in tree.css("a.h-product-card"):
        full_title = card.attributes.get("title") or ""
        title = full_title.removeprefix(_TITLE_PREFIX).strip()
        href = card.attributes.get("href") or ""
        price_node = card.css_first(".h-product-card__price")
        if not title or not href or price_node is None:
            continue
        digits = "".join(ch for ch in price_node.text() if ch.isdigit())
        if not digits:
            continue
        price = int(digits)

        chip = extract_chip(title)
        if not chip:
            continue

        # На витрине отсутствующие товары не отображаются; позитив по умолчанию.
        low = card.text(separator=" ").lower()
        in_stock = not any(marker in low for marker in _OUT_MARKERS)

        offers.append(
            Offer(
                shop=SHOP,
                kind=ItemKind.BUILD if looks_like_build(title) else ItemKind.CARD,
                title=title,
                price=price,
                url=f"{BASE}{href}" if href.startswith("/") else href,
                class_key=class_key(chip, extract_memory_gb(title, chip)),
                part_number=extract_part_number(title),
                chip=chip,
                memory_gb=extract_memory_gb(title, chip),
                brand=extract_brand(title),
                in_stock=in_stock,
            )
        )
    return offers


async def fetch(client) -> list[Offer]:
    """Загружает каталог полным Chromium (channel="chromium") и скроллит,
    пока появляются новые карточки.

    Параметр `client` не используется (интерфейс общий с httpx-парсерами).
    """
    del client  # интерфейс единый с остальными магазинами

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Для halyk нужен Playwright: установите пакет командой "
            "`uv sync --extra dns` и выполните `playwright install chromium`"
        ) from exc

    offers: list[Offer] = []
    seen: set[str] = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chromium")
        ctx = await browser.new_context(user_agent=_USER_AGENT, locale="ru-KZ")
        page = await ctx.new_page()
        try:
            last_error: Exception | None = None
            for _ in range(2):
                try:
                    await page.goto(CATALOG_URL, timeout=_PAGE_TIMEOUT_MS,
                                    wait_until="domcontentloaded")
                    await page.wait_for_selector("a.h-product-card",
                                                 timeout=_PAGE_TIMEOUT_MS)
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001 — сайт отвечает нестабильно
                    last_error = exc
                    await asyncio.sleep(5.0)
            if last_error is not None:
                raise RuntimeError(f"каталог не отдал карточки: {last_error}")
            offers.extend(new_offers(parse(await page.content()), seen))

            stale_rounds = 0
            for _ in range(_MAX_SCROLL_ROUNDS):
                await page.mouse.wheel(0, 4000)
                await asyncio.sleep(_SCROLL_DELAY_S)
                found = new_offers(parse(await page.content()), seen)
                offers.extend(found)
                # Два раунда без новых карточек — каталог исчерпан.
                if not found:
                    stale_rounds += 1
                    if stale_rounds >= 2:
                        break
                else:
                    stale_rounds = 0
        finally:
            await browser.close()
    return offers
