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
        f'<polyline points="{polyline}" fill="none" stroke="#58a6ff" stroke-width="2"/>'
        f'<text x="0" y="{height - 1}" class="axis">{_esc(first_day)}</text>'
        f'<text x="{width}" y="{height - 1}" class="axis" text-anchor="end">'
        f'{_esc(last_day)}</text>'
        f'<text x="0" y="10" class="axis">{_fmt(low)}</text>'
        f'<text x="{width}" y="10" class="axis" text-anchor="end">{_fmt(high)}</text>'
        f'</svg>'
    )


def render(conn: sqlite3.Connection, out_path) -> int:  # noqa: ANN001 — pathlib
    """Собирает HTML и пишет в файл. Возвращает число классов на странице."""
    skip = settings.thresholds.skip_memory_gb
    chips_q = ",".join("?" * len(INTERESTED_CHIPS))
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")

    current = {}
    for row in conn.execute(
        f"""SELECT class_key, MIN(price) AS price, shop
            FROM observations
            WHERE kind = 'card' AND in_stock = 1 AND chip IN ({chips_q})
                  AND (memory_gb IS NULL OR memory_gb > ?)
                  AND class_key IS NOT NULL AND observed_at >= ?
            GROUP BY class_key""",
        (*sorted(INTERESTED_CHIPS), skip, since),
    ):
        current[row["class_key"]] = (row["price"], row["shop"])

    targets = {
        m.class_key: m.target_price
        for m in settings.watchlist
        if m.target_price is not None
    }
    watched = list(settings.watched_class_keys)

    sections: list[str] = []
    for class_key in sorted(current, key=lambda ck: (ck not in watched, ck)):
        price, shop = current[class_key]
        points = _class_daily_medians(conn, class_key)
        spark = _svg_sparkline(points)
        target = targets.get(class_key)
        if target is None:
            target_line = ""
        elif price <= target:
            target_line = f' · цель {_fmt(target)} ₸ <span class="ok">✓ достигнута</span>'
        else:
            target_line = f' · до цели {_fmt(price - target)} ₸'
        sections.append(
            f'<section><h2>{_esc(class_key)}</h2>'
            f'<p class="price">от {_fmt(price)} ₸ · {_esc(shop)}{target_line}</p>'
            f'{spark}</section>'
        )

    leaders: list[tuple[str, str, int, float]] = []
    for row in conn.execute(
        f"""SELECT identity, MAX(observed_at) AS at, price, shop, title, chip, class_key
            FROM observations
            WHERE kind = 'card' AND in_stock = 1 AND chip IN ({chips_q})
                  AND (memory_gb IS NULL OR memory_gb > ?) AND observed_at >= ?
            GROUP BY identity""",
        (*sorted(INTERESTED_CHIPS), skip, since),
    ):
        rating = benchmarks.rating_for(row["class_key"], row["chip"])
        if rating and rating.g3d:
            leaders.append((row["title"], row["shop"], row["price"],
                            row["price"] / rating.g3d))
    leaders.sort(key=lambda item: item[3])
    leader_rows = "".join(
        f"<tr><td>{_esc(title[:70])}</td><td>{_esc(shop)}</td>"
        f"<td>{_fmt(price)} ₸</td>"
        f"<td>{str(round(pp, 1)).replace('.', ',')}</td></tr>"
        for title, shop, price, pp in leaders[:10]
    )

    generated = datetime.now(UTC).strftime("%d.%m.%Y %H:%M UTC")
    page = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gpu-deals: рынок видеокарт Казахстана</title>
<style>
 body {{ background: #0d1117; color: #c9d1d9; font-family: sans-serif;
        max-width: 760px; margin: 0 auto; padding: 1rem; }}
 h1, h2 {{ color: #e6edf3; }}
 section {{ border-top: 1px solid #21262d; padding-top: .5rem; }}
 .price {{ color: #e6edf3; font-size: 1.05rem; }}
 .axis {{ fill: #8b949e; font-size: 10px; }}
 .ok {{ color: #3fb950; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td, th {{ border-bottom: 1px solid #21262d; padding: .3rem .4rem; text-align: left; }}
 footer {{ color: #8b949e; font-size: .85rem; }}
</style></head><body>
<h1>Рынок видеокарт Казахстана</h1>
<p>Минимумы за последние 30 дней по интересующим сериям. Источник — бот
gpu-deals: семь магазинов, обходы по расписанию, цены только по позициям
в наличии.</p>
{"".join(sections)}
<h2>Лидеры по цене за балл PassMark</h2>
<table><tr><th>Модель</th><th>Магазин</th><th>Цена</th><th>₸/балл</th></tr>
{leader_rows}</table>
<footer>Сгенерировано {generated} · обновляется еженедельно</footer>
</body></html>"""

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return len(sections)
