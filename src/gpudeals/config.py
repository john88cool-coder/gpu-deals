"""Настройки: пороги сигналов, бюджеты, watchlist."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Каталог данных: БД и справочник бенчмарков.
DATA_DIR = Path(os.environ.get("GPUDEALS_DATA", Path(__file__).parent / "data"))
DB_PATH = Path(os.environ.get("GPUDEALS_DB", DATA_DIR / "prices.sqlite3"))
BENCHMARKS_CSV = DATA_DIR / "gpu_benchmarks.csv"


@dataclass(frozen=True)
class Thresholds:
    """Пороги, при которых позиция попадает в уведомление.

    Магазинная скидка (oldPrice/discount) сознательно не участвует: у Technodom
    «-18%» стоит на трети каталога, а «-41%» встречается на позиции дороже
    аналога без скидки.
    """

    # «Упало»: цена конкретной модели ниже ожидаемой по тренду.
    drop_pct: float = 3.0
    # «Дешевле аналогов»: ниже медианы по классу «чип + объём памяти».
    below_class_median_pct: float = 7.0
    # Окно тренда в днях. Минимум за всё время не годится: рынок памяти растёт,
    # и старые цены навсегда заглушили бы сигнал.
    trend_window_days: int = 14
    # Минимум наблюдений, до которого сигнал «упало» по позиции не считается.
    min_observations_for_trend: int = 7

    # Мягкий потолок на отдельную карту: выше — не молчим, а помечаем.
    card_budget: int = 600_000
    # Жёсткий потолок на готовую сборку.
    build_budget: int = 1_000_000

    # Тревога о поломке парсера: было >= N позиций, стало 0.
    breakage_min_previous_items: int = 20


@dataclass(frozen=True)
class WatchedModel:
    """Модель для частой проверки.

    `query` — текст запроса для магазинов с поиском (Kaspi), `class_key` — ключ
    класса для фильтрации каталогов, которые отдаются целиком.
    """

    query: str
    class_key: str


@dataclass(frozen=True)
class Settings:
    thresholds: Thresholds = field(default_factory=Thresholds)
    telegram_token: str | None = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = os.environ.get("TELEGRAM_CHAT_ID")
    # Модели для частой проверки (каждые 15-20 минут).
    watchlist: tuple[WatchedModel, ...] = (
        WatchedModel("RTX 5060 Ti 16", "rtx5060ti-16"),
        WatchedModel("RTX 5070 12", "rtx5070-12"),
        WatchedModel("RTX 5070 Ti 16", "rtx5070ti-16"),
        WatchedModel("RX 9070", "rx9070-16"),
        WatchedModel("RX 9070 XT", "rx9070xt-16"),
    )
    request_timeout: float = 25.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    @property
    def watched_class_keys(self) -> frozenset[str]:
        return frozenset(model.class_key for model in self.watchlist)

    @property
    def watched_queries(self) -> tuple[str, ...]:
        return tuple(model.query for model in self.watchlist)


settings = Settings()
