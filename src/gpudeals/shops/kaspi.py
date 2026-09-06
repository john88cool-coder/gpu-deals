"""Парсер Kaspi.

Магазин не отдаёт старую цену: `unitPrice` и `unitSalePrice` всегда равны,
`promo: true` стоит на всех товарах подряд. Поэтому сигнал «упало» по Kaspi
строится не на магазинных скидках, а на нашей собственной истории снимков —
сравнение с медианой предыдущих наблюдений позиции. После накопления ≥7
наблюдений Kaspi работает как полноценный источник алертов (решение
2026-09-06: собственная история заменила отсутствие магазинных старых цен).

Незадокументированный эндпоинт, которым пользуется собственный фронтенд магазина:
`GET /yml/product-view/pl/filters` с заголовком `X-KS-City`. Отдаёт 12 карточек за
запрос, поэтому категория целиком не обходится — только точечные запросы по
watchlist. robots.txt просит Crawl-delay: 10, пауза соблюдается.
"""

from __future__ import annotations

import asyncio
import logging

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

SHOP = "kaspi"
API_URL = "https://kaspi.kz/yml/product-view/pl/filters"
# Алматы. Цены по городам совпадают — проверено на Астане (710000000).
CITY_ID = "750000000"
CRAWL_DELAY = 10.0

log = logging.getLogger(__name__)


def _card_to_offer(card: dict) -> Offer | None:
    title = (card.get("title") or "").strip()
    price = card.get("unitPrice")
    if not title or not price:
        return None

    chip = extract_chip(title)
    if not chip:
        return None

    memory_gb = extract_memory_gb(title, chip)
    shop_link = card.get("shopLink") or ""
    return Offer(
        shop=SHOP,
        kind=ItemKind.BUILD if looks_like_build(title) else ItemKind.CARD,
        title=title,
        price=int(price),
        url=f"https://kaspi.kz{shop_link}" if shop_link.startswith("/") else shop_link,
        class_key=class_key(chip, memory_gb),
        part_number=extract_part_number(title),
        chip=chip,
        memory_gb=memory_gb,
        brand=card.get("brand") or extract_brand(title),
        # shop_old_price намеренно не заполняется: магазин его не публикует.
        in_stock=True,
        sku=str(card.get("id")) if card.get("id") else None,
    )


def parse(payload: dict) -> list[Offer]:
    """Разбирает ответ API в предложения."""
    cards = payload.get("data", {}).get("cards", [])
    offers = (_card_to_offer(card) for card in cards)
    return [offer for offer in offers if offer]


async def search(client, text: str) -> list[Offer]:
    """Один точечный запрос по модели."""
    response = await client.get(
        API_URL,
        params={"text": text, "page": 0, "ui": "d"},
        headers={"X-KS-City": CITY_ID, "Referer": "https://kaspi.kz/shop/search/"},
    )
    response.raise_for_status()
    return parse(response.json())


async def fetch(client, queries: list[str] | None = None) -> list[Offer]:
    """Точечные запросы по watchlist. Категория целиком не обходится."""
    from ..config import settings

    targets = queries if queries is not None else list(settings.watched_queries)
    offers: list[Offer] = []
    seen: set[str] = set()

    for index, query in enumerate(targets):
        if index:
            await asyncio.sleep(CRAWL_DELAY)
        try:
            found = await search(client, query)
        except Exception as exc:  # noqa: BLE001 — один запрос не должен ронять обход
            log.warning("kaspi: запрос %r не удался: %s", query, exc)
            continue
        # Запросы по разным моделям возвращают пересекающиеся выдачи.
        offers.extend(new_offers(found, seen))
    return offers
