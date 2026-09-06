"""Парсер sulpak.kz.

Серверный HTML за Cloudflare, который пропускает запросы с обычным
User-Agent. Каждая карточка отдаёт название, код, цену и бренд в data-атрибутах;
старой цены в плитке нет вовсе — даже у позиций с пометкой скидки.
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

SHOP = "sulpak"
BASE = "https://www.sulpak.kz"
CATALOG_URL = f"{BASE}/f/videokartiy"

# Сколько страниц проходить за один обход. Сортировка по цене не сохраняется
# между запросами стабильно, поэтому берём начало каталога.
_MAX_PAGES = 10
_PAGE_DELAY = 1.5

log = logging.getLogger(__name__)


def parse(html: str) -> list[Offer]:
    tree = HTMLParser(html)
    offers: list[Offer] = []

    for node in tree.css("div.product__item-js"):
        title = (node.attributes.get("data-name") or "").strip()
        raw_price = node.attributes.get("data-price") or ""
        if not title or not raw_price:
            continue
        try:
            price = int(float(raw_price))
        except ValueError:
            continue
        # У позиций «под заказ» в data-price стоит 0.0. Ноль в базе порождает
        # ложные минимумы рынка, поэтому позиция без цены пропускается.
        if price <= 0:
            continue

        chip = extract_chip(title)
        if not chip:
            continue

        link = node.css_first("a[href]")
        href = link.attributes.get("href") if link else None

        # Статус наличия в плитке: «Есть в наличии», «Мало», «Нет в наличии»,
        # «Под заказ». Позитив по умолчанию: если вёрстка сменит подписи,
        # алерты не должны замолчать — потеряем только строку наличия.
        status_node = node.css_first(".product__item-showcase")
        status = status_node.text(strip=True) if status_node else ""
        low = status.lower()
        out_markers = ("нет в наличии", "под заказ", "ожидается")
        in_stock = not any(marker in low for marker in out_markers)

        memory_gb = extract_memory_gb(title, chip)
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
                brand=node.attributes.get("data-brand") or extract_brand(title),
                # Старой цены в плитке нет: поле остаётся пустым, а не нулём.
                in_stock=in_stock,
                stock_note=status or None,
                sku=node.attributes.get("data-code"),
            )
        )
    return offers


def total_pages(html: str) -> int:
    """Число страниц каталога: магазин пишет его в `data-pagesCount` пагинации.

    Нужно потому, что номер за границей каталога не даёт пустого ответа:
    `?page=7` при пяти страницах возвращает последнюю доступную (проверено —
    в ответе стоит `data-currentPage="3"`), и обход «пока страницы отдают
    позиции» не останавливался бы никогда.
    """
    node = HTMLParser(html).css_first("[data-pagesCount]")
    if not node:
        return 1
    # selectolax приводит имена атрибутов к нижнему регистру.
    raw = node.attributes.get("data-pagescount") or ""
    return int(raw) if raw.isdigit() and int(raw) > 0 else 1


async def fetch(client) -> list[Offer]:
    response = await client.get(CATALOG_URL)
    response.raise_for_status()

    seen: set[str] = set()
    offers = new_offers(parse(response.text), seen)

    pages = min(total_pages(response.text), _MAX_PAGES)
    for page in range(2, pages + 1):
        await asyncio.sleep(_PAGE_DELAY)
        try:
            extra = await client.get(CATALOG_URL, params={"page": page})
            extra.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — частичный результат лучше пустого
            log.warning("sulpak: страница %s не загрузилась: %s", page, exc)
            break
        found = new_offers(parse(extra.text), seen)
        # Страница без новых позиций означает, что каталог кончился и магазин
        # повторяет последнюю: дальше идти незачем.
        if not found:
            break
        offers.extend(found)
    return offers
