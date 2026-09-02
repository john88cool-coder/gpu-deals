"""Парсер Technodom.

Каталог — Next.js: готовый JSON лежит в `__NEXT_DATA__` прямо в HTML, поэтому
браузер не нужен. Поля `price`, `oldPrice`, `discount`, `sku`, `has_defectives`
приходят структурно; идентификатор модели стоит в названии в квадратных скобках.

Разбираются два каталога: видеокарты и компьютеры. Второй отдаёт вперемешку
сборки, мониторы и моноблоки, поэтому позиции без распознанного GPU отбрасываются.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from ..models import ItemKind, Offer
from ..normalize import (
    class_key,
    extract_brand,
    extract_chip,
    extract_memory_gb,
    extract_part_number,
    looks_like_build,
)

SHOP = "technodom"
_BASE = "https://www.technodom.kz/catalog/noutbuki-i-komp-jutery"
CATALOG_URL = f"{_BASE}/komplektujuschie/videokarty"
COMPUTERS_URL = f"{_BASE}/komp-jutery-i-monitory"

# Категория компьютеров содержит 205 позиций, из которых сборок с GPU — меньшая
# часть. Ограничиваем обход, чтобы не тянуть страницы мониторов.
_MAX_PAGES = 9

_NEXT_DATA = re.compile(r'__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

log = logging.getLogger(__name__)


def _to_int(value: object) -> int | None:
    if value in (None, "", "0"):
        return None
    try:
        return int(float(str(value).replace(" ", "").replace("\u00a0", "")))
    except (TypeError, ValueError):
        return None


def _product_list(html: str) -> dict:
    match = _NEXT_DATA.search(html)
    if not match:
        return {}
    return (
        json.loads(match.group(1))
        .get("props", {})
        .get("pageProps", {})
        .get("initialState", {})
        .get("productList", {})
    )


def total_pages(html: str) -> int:
    """Сколько всего страниц в категории по данным самой страницы."""
    pagination = _product_list(html).get("paginationData") or {}
    return int(pagination.get("totalPages") or 1)


def parse(html: str, *, builds_only: bool = False) -> list[Offer]:
    """Извлекает предложения из HTML страницы каталога.

    `builds_only` нужен для категории компьютеров: там вперемешку лежат
    мониторы и моноблоки, которые к делу не относятся.
    """
    offers: list[Offer] = []
    for item in _product_list(html).get("items", []):
        title = (item.get("title") or "").strip()
        price = _to_int(item.get("price"))
        if not title or not price:
            continue

        chip = extract_chip(title)
        kind = ItemKind.BUILD if looks_like_build(title) else ItemKind.CARD

        # В категории компьютеров без GPU в названии позиция бесполезна:
        # это монитор, моноблок или сборка со встроенной графикой.
        if builds_only and (kind is not ItemKind.BUILD or not chip):
            continue

        memory_gb = extract_memory_gb(title, chip)
        old_price = _to_int(item.get("oldPrice"))
        # Магазинную «скидку» сохраняем для показа, но решения на ней не строим.
        if old_price is not None and old_price <= price:
            old_price = None

        offers.append(
            Offer(
                shop=SHOP,
                kind=kind,
                title=title,
                price=price,
                url=_product_url(item),
                class_key=class_key(chip, memory_gb),
                part_number=extract_part_number(title),
                chip=chip,
                memory_gb=memory_gb,
                brand=item.get("brand") or extract_brand(title),
                shop_old_price=old_price,
                shop_discount_pct=_to_int(item.get("discount")),
                in_stock=not item.get("isPreorder", False),
                stock_note="предзаказ" if item.get("isPreorder") else None,
                sku=str(item.get("sku")) if item.get("sku") else None,
            )
        )
    return offers


def _product_url(item: dict) -> str:
    uri = item.get("uri") or ""
    return f"https://www.technodom.kz/p/{uri}" if uri else CATALOG_URL


async def _fetch_category(client, url: str, *, builds_only: bool) -> list[Offer]:
    """Загружает все страницы категории."""
    response = await client.get(url)
    response.raise_for_status()
    offers = parse(response.text, builds_only=builds_only)

    pages = min(total_pages(response.text), _MAX_PAGES)
    for page in range(2, pages + 1):
        # Пауза между страницами: магазин не просил, но обход раз в 2 часа
        # не требует спешки.
        await asyncio.sleep(1.0)
        try:
            extra = await client.get(url, params={"page": page})
            extra.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — частичный результат лучше пустого
            log.warning("technodom: страница %s из %s не загрузилась: %s", page, url, exc)
            break
        offers.extend(parse(extra.text, builds_only=builds_only))
    return offers


async def fetch(client) -> list[Offer]:
    """Видеокарты и готовые сборки одним обходом."""
    cards = await _fetch_category(client, CATALOG_URL, builds_only=False)
    builds = await _fetch_category(client, COMPUTERS_URL, builds_only=True)
    return cards + builds
