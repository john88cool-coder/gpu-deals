"""Парсер e-katalog.kz.

Агрегатор: показывает диапазон цен по продавцам («225 740 – 259 600 тг.») и
число предложений. Истории цен нет — проверено на листинге и карточках, поэтому
роль эталона рынка, а не источника алертов (ALERT_SOURCE = False): нижняя
граница диапазона занижена относительно любой отдельной витрины, и алерты от
нее были бы ложными.

Строки каталога — `<tr class="model-short-row">` с идентификатором в `data-idgood`.
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
from .paging import new_offers

SHOP = "e-katalog"
BASE = "https://e-katalog.kz"
CATALOG_URL = f"{BASE}/list/189/"

ALERT_SOURCE = False

_MAX_PAGES = 5
_PAGE_DELAY = 2.0

log = logging.getLogger(__name__)


def _clean_number(text: str) -> int | None:
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def parse(html: str) -> list[Offer]:
    tree = HTMLParser(html)
    offers: list[Offer] = []

    for row in tree.css("tr.model-short-row"):
        link = row.css_first("a.model-short-title")
        if not link:
            continue
        title = link.text(strip=True)
        if not title:
            continue

        chip = extract_chip(title)
        if not chip:
            continue

        # Диапазон «мин – макс тг.» или одна цена; берём минимум.
        price_node = row.css_first("a.price")
        prices = [_clean_number(s.text()) for s in price_node.css("span")] if price_node else []
        prices = [p for p in prices if p]
        if not prices:
            continue
        price = min(prices)

        href = link.attributes.get("href") or ""

        offers.append(
            Offer(
                shop=SHOP,
                kind=ItemKind.BUILD if looks_like_build(title) else ItemKind.CARD,
                title=title,
                price=price,
                url=f"{BASE}{href}" if href else CATALOG_URL,
                class_key=class_key(chip, extract_memory_gb(title, chip)),
                part_number=extract_part_number(title),
                chip=chip,
                memory_gb=extract_memory_gb(title, chip),
                brand=extract_brand(title),
                # Диапазон продавцов — не «старая цена» магазина.
                in_stock=True,
                sku=link.attributes.get("data-idgood"),
            )
        )
    return offers


def total_pages(html: str) -> int:
    """Число страниц из ссылок вида /list/189/N/."""
    numbers = [
        int(m.group(1))
        for m in (re.match(r"/list/189/(\d+)/", href) for href in _page_hrefs(html))
        if m
    ]
    return max(numbers) if numbers else 1


def _page_hrefs(html: str) -> list[str]:
    return [
        node.attributes.get("href") or ""
        for node in HTMLParser(html).css("a[href^='/list/189/']")
    ]


async def fetch(client) -> list[Offer]:
    response = await client.get(CATALOG_URL)
    response.raise_for_status()

    seen: set[str] = set()
    offers = new_offers(parse(response.text), seen)

    pages = min(total_pages(response.text), _MAX_PAGES)
    for page in range(2, pages + 1):
        await asyncio.sleep(_PAGE_DELAY)
        try:
            extra = await client.get(f"{CATALOG_URL}{page}/")
            extra.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — частичный результат лучше пустого
            log.warning("e-katalog: страница %s не загрузилась: %s", page, exc)
            break
        offers.extend(new_offers(parse(extra.text), seen))
    return offers
