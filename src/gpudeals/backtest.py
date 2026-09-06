"""Прогон порогов сигналов по накопленной истории.

Запуск: `uv run python -m gpudeals.backtest` (база — как обычно, из
GPUDEALS_DB или data/prices.sqlite3).

Зачем: пороги «упало» 3% и «дешевле аналогов» 7% выбраны до того, как
появились живые данные. Скрипт прокручивает накопленные дни и показывает,
сколько алертов породила бы каждая комбинация порогов, — числа для решения
«крутить или не крутить» вместо гадания.

Симуляция честно упрощена: день представлен минимумом каждой позиции за этот
день, сигналы считаются на момент дня D по данным ДО D (медианы класса —
за предыдущие 3 дня, тренд позиции — за предыдущие 14 при ≥7 точках).
Магазинная скидка, рестоки и кросс-магазинные строки не симулируются — они
на пороги не влияют.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median

from .config import INTERESTED_CHIPS, settings


def _load_daily_minima(conn: sqlite3.Connection, days: int) -> dict[str, dict[str, int]]:
    """Минимум каждой позиции по дням: {день: {identity: (цена, класс, чип)}}."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    skip = settings.thresholds.skip_memory_gb
    chips_q = ",".join("?" * len(INTERESTED_CHIPS))
    rows = conn.execute(
        f"""SELECT substr(observed_at, 1, 10) AS day, identity, MIN(price) AS price,
                   class_key, chip
            FROM observations
            WHERE kind = 'card' AND in_stock = 1 AND class_key IS NOT NULL
                  AND chip IN ({chips_q}) AND (memory_gb IS NULL OR memory_gb > ?)
                  AND observed_at >= ?
            GROUP BY day, identity""",
        (*sorted(INTERESTED_CHIPS), skip, since),
    )
    daily: dict[str, dict[str, tuple[int, str, str]]] = defaultdict(dict)
    for row in rows:
        daily[row["day"]][row["identity"]] = (row["price"], row["class_key"], row["chip"])
    return daily


def run(
    conn: sqlite3.Connection,
    days: int = 30,
    drop_grid: tuple[int, ...] = (2, 3, 5),
    below_grid: tuple[int, ...] = (5, 7, 10),
) -> str:
    """Возвращает текстовый отчёт: алерты по комбинациям порогов и список
    срабатываний при текущих порогах."""
    t = settings.thresholds
    daily = _load_daily_minima(conn, days)
    if not daily:
        return "Истории пока нет — прогонять пороги не на чем."

    days_sorted = sorted(daily)
    counts = {(d, b): 0 for d in drop_grid for b in below_grid}
    # Дедупликация как в боте: повторный алерт по позиции — только при цене
    # строго ниже последней отправленной. Без этого позиция, «зависшая» ниже
    # медианы, стреляла бы каждый день, и пороги казались бы в разы шумнее.
    # Состояние отдельное на каждую комбинацию порогов.
    dedup_state: dict[tuple[int, int], dict[str, int]] = {
        (d, b): {} for d in drop_grid for b in below_grid
    }
    current = (t.drop_pct, t.below_class_median_pct)
    if current not in dedup_state:
        dedup_state[current] = {}
        counts[current] = 0
    current_fires: list[str] = []

    for index, day in enumerate(days_sorted):
        # Медианы классов за предыдущие 3 дня: минимум позиции за день,
        # затем минимум по идентификатору за окно.
        window = days_sorted[max(0, index - 3):index]
        class_pool: dict[str, list[int]] = defaultdict(list)
        for prev_day in window:
            for _, (price, class_key, _) in daily[prev_day].items():
                class_pool[class_key].append(price)
        class_medians = {
            ck: median(prices) for ck, prices in class_pool.items() if len(prices) >= 3
        }

        # Тренд позиции: дневные минимумы за предыдущие 14 дней.
        trend_window = days_sorted[max(0, index - 14):index]
        trend_pool: dict[str, list[int]] = defaultdict(list)
        for prev_day in trend_window:
            for identity, (price, _, _) in daily[prev_day].items():
                trend_pool[identity].append(price)

        for identity, (price, class_key, _) in daily[day].items():
            class_med = class_medians.get(class_key)
            trend_points = trend_pool.get(identity, [])
            trend_med = median(trend_points) if len(trend_points) >= 7 else None

            below_pct = (
                (class_med - price) / class_med * 100
                if class_med and class_med > price else 0.0
            )
            drop_pct = (
                (trend_med - price) / trend_med * 100
                if trend_med and trend_med > price else 0.0
            )

            for d in drop_grid:
                for b in below_grid:
                    state = dedup_state[(d, b)]
                    if (drop_pct >= d or below_pct >= b) and (
                        identity not in state or price < state[identity]
                    ):
                        counts[(d, b)] += 1
                        state[identity] = price
                        if (d, b) == current:
                            price_str = f"{price:,}".replace(",", " ")
                            current_fires.append(
                                f"  {day} {identity}: {price_str} ₸ "
                                f"(упало {drop_pct:.0f}%, "
                                f"дешевле аналогов {below_pct:.0f}%)"
                            )

    lines = [
        f"Прогон за {len(days_sorted)} дн., позиций в день — как в живой базе.",
        "",
        "Алертов было бы при порогах (упало / дешевле аналогов):",
    ]
    for d in drop_grid:
        row = "  ".join(f"{b:>2}%: {counts[(d, b)]:4}" for b in below_grid)
        lines.append(f"  упало ≥{d}%:  {row}")
    lines.append("")
    if current_fires:
        lines.append(
            f"Срабатывания при текущих порогах "
            f"({t.drop_pct}%/{t.below_class_median_pct}%): {len(current_fires)}"
        )
        lines.extend(current_fires[-20:])
    else:
        lines.append(
            f"При текущих порогах ({t.drop_pct}%/{t.below_class_median_pct}%) "
            "срабатываний не было."
        )
    return "\n".join(lines)


def main() -> None:
    import logging

    from .config import DB_PATH
    from .storage import connect

    logging.basicConfig(level=logging.INFO)
    with connect(DB_PATH) as conn:
        print(run(conn))


if __name__ == "__main__":
    main()
