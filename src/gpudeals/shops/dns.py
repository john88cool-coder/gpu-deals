"""Парсер dns-shop.kz — headless-браузер (Playwright).

Единственный магазин из семи, где HTTP-запросом товары не получить: каталог
отдаёт Cloudflare-челлендж, а товары рендерятся на клиенте. С обычным
User-Agent и браузером страница открывается, поэтому Playwright используется
только здесь; остальные шесть магазинов ходят httpx-ом.

Раздел уценки `/catalog/markdown/` не парсится: в robots.txt стоит
`Disallow: /catalog/markdown/*`. Основной каталог разрешён.

Старой цены в листинге DNS нет; наличие берётся из метки «В наличии» /
«При заказе».
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

SHOP = "dns"
CATALOG_URL = "https://www.dns-shop.kz/catalog/17a89aab16404e77/videokarty/"

# Страниц в категории ~14; ограничиваем, чтобы обход укладывался в разумное
# время: каждая страница — это загрузка в браузере.
_MAX_PAGES = 6
_PAGE_TIMEOUT_MS = 45_000
_WAIT_PRODUCTS_MS = 25_000

log = logging.getLogger(__name__)


def _int_from_text(text: str) -> int | None:
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def parse(html: str) -> list[Offer]:
    """Разбирает отрендеренный браузером HTML каталога."""
    tree = HTMLParser(html)
    offers: list[Offer] = []

    for card in tree.css("div.catalog-product"):
        name = card.css_first("a.catalog-product__name")
        title = (name.attributes.get("title") or "").strip() if name else ""
        if not title:
            continue

        chip = extract_chip(title)
        if not chip:
            continue

        price_node = card.css_first(".product-buy__price")
        price = _int_from_text(price_node.text() if price_node else "")
        if not price:
            continue

        availability = card.css_first('[class*="available"]')
        stock_text = availability.text(strip=True) if availability else ""
        in_stock = "в наличии" in stock_text.lower()

        href = name.attributes.get("href") if name else None
        memory_gb = extract_memory_gb(title, chip)

        offers.append(
            Offer(
                shop=SHOP,
                kind=ItemKind.BUILD if looks_like_build(title) else ItemKind.CARD,
                title=title,
                price=price,
                url=f"https://www.dns-shop.kz{href}" if href else CATALOG_URL,
                class_key=class_key(chip, memory_gb),
                part_number=extract_part_number(title),
                chip=chip,
                memory_gb=memory_gb,
                brand=extract_brand(title),
                # Старой цены в листинге DNS нет.
                in_stock=in_stock,
                stock_note=stock_text or None,
            )
        )
    return offers


def total_pages(html: str) -> int:
    """«страница 1 из 14» в заголовке — самый надёжный источник числа страниц."""
    import re

    m = re.search(r"страница\s+\d+\s+из\s+(\d+)", html)
    return int(m.group(1)) if m else 1


async def fetch(client) -> list[Offer]:
    """Загружает каталог в headless Chromium и разбирает отрендеренные страницы.

    Параметр `client` не используется (интерфейс общий с httpx-парсерами).
    """
    del client  # интерфейс единый с остальными магазинами

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Для DNS нужен Playwright: установите пакет командой "
            "`uv sync --extra dns` и выполните `playwright install chromium`"
        ) from exc

    offers: list[Offer] = []
    seen: set[str] = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Служебный User-Agent Playwright («HeadlessChrome») вызывает челлендж;
        # подменяем на обычный.
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        )
        try:
            for page_number in range(1, _MAX_PAGES + 1):
                url = CATALOG_URL if page_number == 1 else f"{CATALOG_URL}?page={page_number}"
                try:
                    await _goto_with_retry(page, url)
                except Exception as exc:  # noqa: BLE001
                    if page_number == 1:
                        raise
                    log.warning("dns: страница %s не загрузилась: %s", page_number, exc)
                    break
                rendered = parse(await page.content())
                found = new_offers(rendered, seen)
                offers.extend(found)
                # Страница целиком из уже виденных позиций означает, что каталог
                # повторяется: дальше идти незачем.
                if not found or len(rendered) < 12:
                    break
                await asyncio.sleep(2.0)
        finally:
            await browser.close()
    return offers


async def _goto_with_retry(page, url: str, attempts: int = 2) -> None:
    """Загружает страницу и ждёт не только карточек, но и цен.

    Карточки рендерятся сразу, а цены приходят отдельным запросом, который на
    страницах после первой срабатывает нестабильно; помогает прокрутка в
    несколько приёмов.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            await page.goto(url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_selector("div.catalog-product", timeout=_WAIT_PRODUCTS_MS)
            try:
                await page.wait_for_selector(
                    ".product-buy__price", timeout=_WAIT_PRODUCTS_MS
                )
            except Exception:  # noqa: BLE001 — цены ленивые: прокручиваем списком
                for _ in range(4):
                    await page.mouse.wheel(0, 2400)
                    await asyncio.sleep(1.5)
                    if await page.locator(".product-buy__price").count() > 0:
                        break
                else:
                    await page.wait_for_selector(
                        ".product-buy__price", timeout=_WAIT_PRODUCTS_MS
                    )
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(3.0)
    raise RuntimeError(f"страница не отдала цены за {attempts} попытки: {last_error}")
