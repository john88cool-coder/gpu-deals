"""Парсеры магазинов.

Каждый модуль экспортирует SHOP, parse() и fetch(client). Необязательный флаг
ALERT_SOURCE = False означает, что магазин служит эталоном рынка, но собственных
уведомлений не порождает (так работает Kaspi: он не публикует старую цену).
"""

from . import dns, ekatalog, forcecom, kaspi, shop_kz, sulpak, technodom

REGISTRY = {
    technodom.SHOP: technodom,
    shop_kz.SHOP: shop_kz,
    sulpak.SHOP: sulpak,
    forcecom.SHOP: forcecom,
    ekatalog.SHOP: ekatalog,
    kaspi.SHOP: kaspi,
    dns.SHOP: dns,
}


def is_alert_source(module) -> bool:
    return getattr(module, "ALERT_SOURCE", True)


__all__ = [
    "REGISTRY",
    "dns",
    "ekatalog",
    "forcecom",
    "is_alert_source",
    "kaspi",
    "shop_kz",
    "sulpak",
    "technodom",
]
