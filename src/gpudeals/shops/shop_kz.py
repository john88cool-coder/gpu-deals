"""Парсер shop.kz.

Bitrix, серверный HTML. Каждая карточка несёт JSON в атрибуте `data-product` с
ценой, брендом, партномером (`item_variant`) и наличием — этого достаточно, а
разбор вёрстки нужен только для старой цены (`.old_price`).

Каталог видеокарт — 58 страниц по 28 позиций, поэтому обходится не целиком:
берутся первые страницы отсортированного по цене каталога и фильтр RTX 50.
"""

from __future__ import annotations

import asyncio
import json
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

SHOP = "shop.kz"
BASE = "https://shop.kz"
CATALOG_URL = f"{BASE}/offers/videokarty/"
# Фильтр из задачи: NVIDIA RTX 50. Отдаёт 8 страниц вместо 58.
RTX50_URL = f"{CATALOG_URL}filter/vid_chipset-is-nvidia/video_series-is-geforce_rtx_50_series/apply/"

# Полный каталог — 58 страниц; столько за раз не нужно. Актуальные поколения
# укладываются в фильтр RTX 50, остальное подбирается первыми страницами.
_MAX_PAGES = 8
_PAGE_DELAY = 1.5

log = logging.getLogger(__name__)


def _price_from_text(text: str | None) -> int | None:
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def parse(html: str) -> list[Offer]:
    """Извлекает предложения со страницы каталога."""
    tree = HTMLParser(html)
    offers: list[Offer] = []

    for card in tree.css(".bx_catalog_item"):
        try:
            data = json.loads(card.attributes.get("data-product") or "{}")
        except json.JSONDecodeError:
            continue

        title = (data.get("item_name") or "").strip()
        price = data.get("price")
        if not title or not price:
            continue

        chip = extract_chip(title)
        if not chip:
            continue

        link = card.css_first("link[itemprop=url]")
        href = link.attributes.get("href") if link else None
        old_node = card.css_first(".old_price span")
        old_price = _price_from_text(old_node.text() if old_node else None)
        if old_price is not None and old_price <= price:
            old_price = None

        memory_gb = extract_memory_gb(title, chip)
        # `item_variant` — партномер производителя, у shop.kz он заполнен почти
        # всегда и точнее, чем разбор названия. Пробелы убираем, чтобы
        # сопоставлять с другими магазинами.
        variant = (data.get("item_variant") or "").strip()
        variant = variant.upper().replace(" ", "") if variant and any(c.isdigit() for c in variant) else None

        available = data.get("dimension3") == "available"
        offers.append(
            Offer(
                shop=SHOP,
                kind=ItemKind.BUILD if looks_like_build(title) else ItemKind.CARD,
                title=title,
                price=int(price),
                url=f"{BASE}{href}" if href else CATALOG_URL,
                class_key=class_key(chip, memory_gb),
                part_number=variant or extract_part_number(title),
                chip=chip,
                memory_gb=memory_gb,
                brand=data.get("item_brand") or extract_brand(title),
                shop_old_price=old_price,
                in_stock=available,
                stock_note=None if available else "нет в наличии",
                sku=str(data.get("item_id")) if data.get("item_id") else None,
            )
        )
    return offers


def total_pages(html: str) -> int:
    """Число страниц по блоку пагинации Bitrix."""
    node = HTMLParser(html).css_first(".bx-pagination")
    if not node:
        return 1
    numbers = [int(t) for t in node.text(separator=" ").split() if t.isdigit()]
    return max(numbers) if numbers else 1


async def _fetch_pages(client, url: str, seen: set[str]) -> list[Offer]:
    response = await client.get(url)
    response.raise_for_status()
    offers = new_offers(parse(response.text), seen)

    pages = min(total_pages(response.text), _MAX_PAGES)
    for page in range(2, pages + 1):
        await asyncio.sleep(_PAGE_DELAY)
        try:
            extra = await client.get(url, params={"PAGEN_1": page})
            extra.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — частичный результат лучше пустого
            log.warning("shop.kz: страница %s не загрузилась: %s", page, exc)
            break
        offers.extend(new_offers(parse(extra.text), seen))
    return offers


async def fetch(client) -> list[Offer]:
    seen: set[str] = set()
    offers = await _fetch_pages(client, RTX50_URL, seen)
    # Первая страница общего каталога отсортирована так, что там попадаются
    # прошлые поколения с глубокими уценками.
    await asyncio.sleep(_PAGE_DELAY)
    try:
        response = await client.get(CATALOG_URL)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("shop.kz: общий каталог не загрузился: %s", exc)
        return offers
    offers.extend(new_offers(parse(response.text), seen))
    return offers
