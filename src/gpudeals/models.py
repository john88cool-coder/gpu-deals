"""Модель товара, единая для видеокарт и готовых сборок."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ItemKind(str, Enum):
    """Тип товара. Медианы по классу считаются раздельно по типу: сборка за
    870 000 ₸ иначе задерёт медиану класса «RTX 5070 / 12 ГБ» и настоящие
    скидки на карты перестанут выделяться."""

    CARD = "card"
    BUILD = "build"


class MatchLevel(str, Enum):
    """Насколько уверенно позиция сопоставлена с другими.

    PART_NUMBER — та же самая модель, основание для сигнала «упало».
    CLASS — только чип и объём памяти, основание для «дешевле аналогов».
    """

    PART_NUMBER = "part_number"
    CLASS = "class"


@dataclass(frozen=True)
class Offer:
    """Одно предложение магазина на момент обхода."""

    shop: str
    kind: ItemKind
    title: str
    price: int
    url: str
    # Ключ класса: нормализованный чип + объём памяти, например "rtx5070ti-16".
    class_key: str | None = None
    # Партномер производителя, если удалось извлечь: "GV-N5070WF3OC-12GD".
    part_number: str | None = None
    chip: str | None = None
    memory_gb: int | None = None
    brand: str | None = None
    # Что магазин называет старой ценой. Хранится для показа, но не для решений.
    shop_old_price: int | None = None
    shop_discount_pct: int | None = None
    in_stock: bool = True
    stock_note: str | None = None
    sku: str | None = None

    @property
    def match_level(self) -> MatchLevel:
        return MatchLevel.PART_NUMBER if self.part_number else MatchLevel.CLASS

    @property
    def identity(self) -> str:
        """Ключ позиции для истории цен и дедупликации уведомлений.

        Привязан к магазину: одна и та же карта у Technodom и Kaspi — это два
        разных предложения. Без магазина в ключе их цены смешивались бы в одну
        историю, а уведомление о более дешёвом из них подавлялось бы как повтор.
        """
        if self.part_number:
            return f"{self.shop}:pn:{self.part_number.lower()}"
        if self.sku:
            return f"{self.shop}:sku:{self.sku}"
        return f"{self.shop}:title:{self.title.lower()}"
