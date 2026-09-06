"""Парсер forcecom.kz.

Bitrix (aspro-lite), серверный HTML. Карточки размечены microdata
schema.org/Product, название лежит в `catalog-block__info-title`, цена — в
`itemprop=price`. Старой цены и артикула в листинге нет: поля `PRICEOLD` и
`ARTICLE` приходят со значением null.
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

SHOP = "forcecom"
BASE = "https://forcecom.kz"
CATALOG_URL = f"{BASE}/catalog/graphics-cards/"

# robots.txt просит Crawl-delay: 5 — выдерживаем не меньше.
CRAWL_DELAY = 5.0
_MAX_PAGES = 10

log = logging.getLogger(__name__)


def _int_from_text(text: str | None) -> int | None:
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def parse(html: str) -> list[Offer]:
    tree = HTMLParser(html)
    offers: list[Offer] = []

    for block in tree.css('[itemtype="http://schema.org/Product"]'):
        name_node = block.css_first(".catalog-block__info-title") or block.css_first(
            "a.switcher-title"
        )
        title = name_node.text(strip=True) if name_node else ""
        if not title:
            continue

        # В названии forcecom указывает память и тип памяти в квадратных скобках:
        # «[2 ГБ, GDDR3, ...]» — пригодится, если объём не виден иначе.
        price_node = block.css_first('meta[itemprop="price"]')
        price = _int_from_text(price_node.attributes.get("content") if price_node else None)
        if not price:
            continue

        chip = extract_chip(title)
        if not chip:
            continue

        link = block.css_first("a[href*='/model/']")
        href = link.attributes.get("href") if link else None
        memory_gb = extract_memory_gb(title, chip)

        # Статус наличия — span.js-replace-status («instock» / «Нет в наличии»).
        # Позитив по умолчанию: живого примера отсутствия в категории карт пока
        # не встречалось, и если классы сменятся, алерты не должны замолчать.
        status_node = block.css_first('[class*="js-replace-status"]')
        status = status_node.text(strip=True) if status_node else ""
        marker = status.lower()
        if status_node:
            marker += " " + (status_node.attributes.get("class") or "").lower()
        out_markers = ("нет в наличии", "под заказ", "ожидается", "outstock", "underorder")
        in_stock = not any(m in marker for m in out_markers)

        offers.append(
            Offer(
                shop=SHOP,
                kind=ItemKind.BUILD if looks_like_build(title) else ItemKind.CARD,
                title=title,
                price=price,
                url=f"{BASE}{href}" if href else CATALOG_URL,
                class_key=class_key(chip, memory_gb),
                part_number=extract_part_number(title),
                chip=chip,
                memory_gb=memory_gb,
                brand=extract_brand(title),
                # PRICEOLD в листинге всегда null — старой цены магазин не отдаёт.
                in_stock=in_stock,
                stock_note=status or None,
            )
        )
    return offers


def total_pages(html: str) -> int:
    node = HTMLParser(html).css_first("[class*=pagination]")
    if not node:
        return 1
    numbers = [int(t) for t in node.text(separator=" ").split() if t.isdigit()]
    return max(numbers) if numbers else 1


async def fetch(client) -> list[Offer]:
    response = await client.get(CATALOG_URL)
    response.raise_for_status()

    # Магазин переставляет товары между страницами между запросами: обход
    # страниц 1-10 подряд дал 200 позиций и 194 уникальных названия. Без учёта
    # уже виденных identity эти шесть попали бы в историю дважды за обход.
    seen: set[str] = set()
    offers = new_offers(parse(response.text), seen)

    pages = min(total_pages(response.text), _MAX_PAGES)
    for page in range(2, pages + 1):
        await asyncio.sleep(CRAWL_DELAY)
        try:
            extra = await client.get(CATALOG_URL, params={"PAGEN_1": page})
            extra.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — частичный результат лучше пустого
            log.warning("forcecom: страница %s не загрузилась: %s", page, exc)
            break
        offers.extend(new_offers(parse(extra.text), seen))
    return offers
