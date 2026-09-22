"""Статический дашборд рынка: HTML с графиками медиан за 30 дней.

Генерируется командой `gpu-deals render-dashboard --out путь` и публикуется
еженедельным workflow на GitHub Pages. Никаких внешних библиотек: графики —
инлайн-SVG, страница работает без JavaScript. Данные читаются из локальной
базы, сетевых запросов нет.
"""

from __future__ import annotations

import html
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from . import benchmarks
from .config import INTERESTED_CHIPS, settings


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _fmt(value: int | float) -> str:
    return f"{value:,}".replace(",", " ")


def _class_daily_medians(
    conn: sqlite3.Connection, class_key: str, days: int = 30
) -> list[tuple[str, int]]:
    """Медиана класса по дням: минимум каждой позиции за день, потом медиана."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT substr(observed_at, 1, 10) AS day, identity, MIN(price) AS price
           FROM observations
           WHERE kind = 'card' AND class_key = ? AND in_stock = 1
                 AND observed_at >= ?
           GROUP BY day, identity""",
        (class_key, since),
    )
    per_day: dict[str, list[int]] = {}
    for row in rows:
        per_day.setdefault(row["day"], []).append(row["price"])
    return sorted((day, int(median(prices))) for day, prices in per_day.items())


def _svg_sparkline(points: list[tuple[str, int]], width: int = 640, height: int = 120) -> str:
    """Полилиния цены по дням. Пустых дней между точками не делаем: ось X —
    индекс точки, даты подписаны по краям."""
    if len(points) < 2:
        return ""
    prices = [p for _, p in points]
    low, high = min(prices), max(prices)
    span = (high - low) or 1
    step_x = width / (len(points) - 1)
    coords = [
        (i * step_x, height - 12 - (p - low) / span * (height - 24))
        for i, (_, p) in enumerate(points)
    ]
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    first_day, last_day = points[0][0], points[-1][0]
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Динамика цены">'
        f'<polyline points="{polyline}" fill="none" class="chart-line" stroke="currentColor" stroke-width="2"/>'
        f'<text x="0" y="{height - 1}" class="axis">{_esc(first_day)}</text>'
        f'<text x="{width}" y="{height - 1}" class="axis" text-anchor="end">'
        f'{_esc(last_day)}</text>'
        f'<text x="0" y="10" class="axis">{_fmt(low)}</text>'
        f'<text x="{width}" y="10" class="axis" text-anchor="end">{_fmt(high)}</text>'
        f'</svg>'
    )


def render(conn: sqlite3.Connection, out_path) -> int:
    """Build the interactive, standalone dashboard from a read-only snapshot."""
    from .dashboard_page import render_page
    return render_page(conn, Path(out_path))
