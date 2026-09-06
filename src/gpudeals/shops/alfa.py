"""Парсер alfa.kz — доска объявлений с витриной видеокарт.

Сайт за анти-ботом Anubis (proof-of-work на JavaScript): обычный HTTP-запрос
получает страницу «Making sure you're not a bot!», браузер проходит челлендж
сам. Поэтому используется Playwright с обычным User-Agent (проверено 2026-09-06).

Разметка серверная, карточки размечены microdata schema.org/Product:
цена — в `meta[itemprop=price]`, заголовок — в h2. Память в названиях пишется
как «6144 Mb» — перед нормализацией переводится в гигабайты, иначе класс
(чип + объём) не строится.

Пагинация — /page2, /page3… Категория целиком — 24 страницы и в ней много
древних карт; обходим первые страницы с запасом, фильтры интересов владельца
отсекают лишнее на уровне crawler.
"""

from __future__ import annotations

import asyncio
import logging
import re

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

SHOP = "alfa"
BASE = "https://alfa.kz"
CATALOG_URL = f"{BASE}/parts/video-cards/"
BUILDS_URL = f"{BASE}/parts/computers/"

# Анти-бот Anubis пропускает и обычный headless; задержка — на случай
# медленного proof-of-work при первом визите.
_PAGE_TIMEOUT_MS = 60_000
_MAX_PAGES = 12
_PAGE_DELAY_S = 2.0

log = logging.getLogger(__name__)

# «6144 Mb» → гигабайты: без этого объём не извлекается и класс не строится.
_MB_MEMORY = re.compile(r"(\d{3,5})\s?Mb", re.I)


def _memory_to_gb(title: str) -> str:
    """Заменяет «8192 Mb» на «8 ГБ» — нормализатор понимает только ГБ/Gb."""

    def _convert(match: re.Match) -> str:
        return f"{round(int(match.group(1)) / 1024)} ГБ"

    return _MB_MEMORY.sub(_convert, title)


def parse(html: str, builds_only: bool = False) -> list[Offer]:
    """Разбирает отрендеренную страницу каталога alfa.kz.

    На витрине сборок (`builds_only=True`) всё с чипом — сборка: заголовки
    вида «PULSER Advanced 383 …» не содержат маркеров сборки, и по ним
    позиция ушла бы в карты, завысив их медианы.
    """
    tree = HTMLParser(html)
    offers: list[Offer] = []
    for block in tree.css('div[data-role="product"]'):
        title_node = block.css_first("h2")
        title = title_node.text(strip=True) if title_node else ""
        link = block.css_first("h2 a")
        href = link.attributes.get("href") if link else None
        price_meta = block.css_first('meta[itemprop="price"]')
        if not title or not href or price_meta is None:
            continue
        raw_price = price_meta.attributes.get("content") or ""
        try:
            price = int(float(raw_price))
        except ValueError:
            continue
        if price <= 0:
            continue

        chip = extract_chip(title)
        if not chip:
            continue

        # Статус наличия: на витрине встречается «Нет в наличии». Позитив по
        # умолчанию — смена вёрстки не должна молча отключить алерты.
        low = block.text(separator=" ").lower()
        out_markers = ("нет в наличии", "под заказ", "ожидается")
        in_stock = not any(marker in low for marker in out_markers)

        normalized = _memory_to_gb(title)
        memory_gb = extract_memory_gb(normalized, chip)
        is_build = looks_like_build(normalized) or builds_only
        # Последний сегмент ссылки — стабильный id объявления: identity не
        # зависит от правок названия.
        sku = href.rstrip("/").rsplit("/", 1)[-1] or None
        offers.append(
            Offer(
                shop=SHOP,
                kind=ItemKind.BUILD if is_build else ItemKind.CARD,
                title=title,
                price=price,
                url=href if href.startswith("http") else f"{BASE}{href}",
                class_key=class_key(chip, memory_gb),
                part_number=extract_part_number(normalized),
                chip=chip,
                memory_gb=memory_gb,
                brand=extract_brand(title),
                in_stock=in_stock,
                sku=sku,
            )
        )
    return offers


def total_pages(html: str) -> int:
    """Число страниц каталога по ссылкам пагинации."""
    pages = [int(n) for n in re.findall(r'href="[^"]*page(\d+)', html)]
    return max(pages) if pages else 1


def _page_url(page: int) -> str:
    return CATALOG_URL if page == 1 else f"{CATALOG_URL}page{page}/"


async def fetch(client) -> list[Offer]:
    """Видеокарты и готовые сборки (витрина computers) одним обходом."""
    del client  # интерфейс единый с остальными магазинами

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Для alfa нужен Playwright: установите пакет командой "
            "`uv sync --extra dns` и выполните `playwright install chromium`"
        ) from exc

    offers: list[Offer] = []
    seen: set[str] = set()
    from .paging import new_offers

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await _goto_with_retry(page, _page_url(1))
            offers.extend(new_offers(parse(await page.content()), seen))
            pages = min(max(total_pages(await page.content()), 5), _MAX_PAGES)
            log.info("alfa: каталог заявляет %s страниц, обходим %s",
                     total_pages(await page.content()), pages)
            for page_number in range(2, pages + 1):
                await asyncio.sleep(_PAGE_DELAY_S)
                try:
                    await _goto_with_retry(page, _page_url(page_number))
                    offers.extend(new_offers(parse(await page.content()), seen))
                except Exception as exc:  # noqa: BLE001 — частичный результат лучше пустого
                    log.warning("alfa: страница %s не загрузилась: %s", page_number, exc)
                    break

            # Готовые сборки: витрина computers, первая страница с запасом.
            await _goto_with_retry(page, BUILDS_URL)
            offers.extend(new_offers(parse(await page.content(), builds_only=True), seen))
        finally:
            await browser.close()
    return offers


async def _goto_with_retry(page, url: str, attempts: int = 2) -> None:
    """Загружает страницу и ждёт карточки.

    Antibot Anubis сначала отдаёт челлендж и решает его на JavaScript,
    перезагружая страницу — ожидание селектора переживает перезагрузку.
    """
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            await page.goto(url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_selector('div[data-role="product"]',
                                         timeout=_PAGE_TIMEOUT_MS)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(3.0)
    raise RuntimeError(f"страница не отдала карточки за {attempts} попытки: {last_error}")
