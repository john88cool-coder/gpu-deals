"""Presentation only: never sends alerts or modifies the price database."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import benchmarks
from .bench_tiers import chip_bench, price_per_rel, tier_badge
from .config import INTERESTED_CHIPS, settings
from .dashboard import _class_daily_medians, _esc, _fmt, _svg_sparkline


def safe_url(value: str | None) -> str:
    try:
        return value if value and urlsplit(value).scheme in ("http", "https") and urlsplit(value).netloc else ""
    except ValueError:
        return ""


def history_rows(points) -> str:
    rows = []
    for i, point in enumerate(points):
        change = (point["price"] / points[i - 1]["price"] - 1) * 100 if i and points[i - 1]["price"] > 0 else None
        delta = "—" if change is None else "0%" if change == 0 else f"{change:+.1f}%".replace(".", ",")
        tone = "down" if change is not None and change < 0 else "up" if change is not None and change > 0 else ""
        date = point["observed_at"]
        rows.append(
            f'<tr><td><time datetime="{_esc(date)}">{_esc(date[:16].replace("T", " "))}</time></td>'
            f'<td>{_fmt(point["price"])} ₸</td><td class="{tone}">{delta}</td></tr>'
        )
    return "".join(reversed(rows[-5:]))


def _badge(kind: str, text: str) -> str:
    return f'<span class="badge badge--{kind}">{_esc(text)}</span>'


from .report import class_label, shop_label  # noqa: E402


def render_page(conn, out: Path) -> int:
    now = datetime.now(UTC)
    since = (now - timedelta(days=30)).isoformat(timespec="seconds")
    fresh = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    chips = sorted(INTERESTED_CHIPS)

    rows = conn.execute(
        f"""WITH ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY identity ORDER BY observed_at DESC, id DESC) AS rn
        FROM observations WHERE observed_at >= ?)
        SELECT * FROM ranked WHERE rn = 1 AND price > 0 AND chip IN ({','.join('?' for _ in chips)})
        AND (memory_gb IS NULL OR memory_gb > ?) ORDER BY price""",
        (since, *chips, settings.thresholds.skip_memory_gb),
    ).fetchall()

    targets = {m.class_key: m.target_price for m in settings.watchlist if m.target_price is not None}
    # watchlist order for progress
    watch_order = [m.class_key for m in settings.watchlist]

    cards: list[str] = []
    leaders: list[tuple[float, object]] = []
    json_items: list[dict] = []

    # precompute class stats for subtitle
    class_counts: dict[str, int] = {}
    for r in rows:
        if r["class_key"]:
            class_counts[r["class_key"]] = class_counts.get(r["class_key"], 0) + 1

    for row in rows:
        target = targets.get(row["class_key"]) if row["kind"] == "card" else None
        reached = bool(target and row["price"] <= target and row["in_stock"] and row["observed_at"] >= fresh)
        is_fresh = row["observed_at"] >= fresh
        observations = conn.execute(
            """SELECT observed_at, price FROM observations
            WHERE identity = ? AND kind = ? AND price > 0 AND in_stock = 1 AND observed_at >= ?
            ORDER BY observed_at DESC, id DESC LIMIT 6""",
            (row["identity"], row["kind"], since),
        ).fetchall()
        points = list(reversed(observations))
        rating = benchmarks.rating_for(row["class_key"], row["chip"]) if row["kind"] == "card" else None
        pp = row["price"] / rating.g3d if rating and rating.g3d else None
        if pp and row["in_stock"] and is_fresh:
            leaders.append((pp, row))

        # status
        if not row["in_stock"]:
            status = "Нет в наличии"
            status_kind = "bad"
        elif not is_fresh:
            status = "Требует проверки"
            status_kind = "warn"
        else:
            status = "В наличии"
            status_kind = "good"

        label = "Видеокарта" if row["kind"] == "card" else "Готовая сборка"
        image = safe_url(row["image_url"]) if "image_url" in row.keys() else ""
        photo = f'<img src="{_esc(image)}" alt="{_esc(row["title"])}" loading="lazy" referrerpolicy="no-referrer">' if image else ""
        visual = (
            f'<div class="visual {"has-image" if image else ""}">{photo}'
            f'<div class="photo-fallback"><span class="chip-icon" aria-hidden="true">▦</span>'
            f"<strong>{_esc((row['chip'] or 'gpu').upper())}</strong><span>Фото пока не сохранено</span></div>"
            f'<div class="visual-actions">'
            f'<button type="button" class="icon-btn fav" data-fav="{_esc(row["identity"])}" aria-label="В избранное" title="В избранное">☆</button>'
            f'<label class="icon-check"><input type="checkbox" data-compare="{_esc(row["identity"])}"><span>Сравнить</span></label>'
            f"</div></div>"
        )

        # badges row — includes tier badge from composite bench_tiers
        tier_info = tier_badge(row["chip"])
        ppr = price_per_rel(row["price"], row["chip"])
        badges = []
        if tier_info:
            badges.append(_badge(f"tier-{tier_info[1]}", tier_info[0]))
        if reached:
            badges.append(_badge("good", "Цель ✓"))
        elif target and is_fresh and row["in_stock"]:
            diff = row["price"] - target
            badges.append(_badge("soft", f"До цели {_fmt(diff)} ₸"))
        # show both synthetic and game-relative value (keeps old ₸/балл for compat)
        if pp is not None:
            badges.append(_badge("accent", f"{pp:.1f} ₸/балл".replace(".", ",")))
        if not is_fresh and row["in_stock"]:
            badges.append(_badge("warn", "Старше 24ч"))
        if not row["in_stock"]:
            badges.append(_badge("bad", "Нет в наличии"))
        badges_html = f'<div class="badges">{"".join(badges)}</div>' if badges else ""

        target_text = ""
        if target:
            if reached:
                target_text = f'<p class="target down">Цель {_fmt(target)} ₸ · достигнута</p>'
            else:
                # show diff only if fresh
                if row["price"] > target:
                    target_text = f'<p class="target">Цель {_fmt(target)} ₸ · разница {_fmt(row["price"] - target)} ₸</p>'
                else:
                    target_text = f'<p class="target">Цель {_fmt(target)} ₸ · проверьте наличие</p>'

        # composite metric line: PassMark + game-rel perf
        bench = chip_bench(row["chip"])
        parts: list[str] = []
        if rating:
            parts.append(f"{_fmt(rating.g3d)} PassMark")
        if bench:
            parts.append(f"индекс {bench.rel_perf:.0f} (5070 Ti = 100)")
        if pp is not None:
            parts.append(f"{pp:.1f} ₸/балл".replace(".", ","))
        if bench and bench.tier_label:
            parts.append(bench.tier_label)
        perf = f'<p class="metric">{" · ".join(parts)}</p>' if parts else '<p class="metric muted">Нет оценки</p>'
        url = safe_url(row["url"])
        link = f'<a class="store-link" href="{_esc(url)}" target="_blank" rel="noopener noreferrer">В магазин ↗</a>' if url else '<span class="muted">Ссылка недоступна</span>'
        old = (
            f'<p class="old">До скидки: <del>{_fmt(row["shop_old_price"])} ₸</del> · <span>не проверено</span></p>'
            if row["shop_old_price"] and row["shop_old_price"] > row["price"]
            else ""
        )
        hist = (
            '<table class="history"><caption class="sr-only">История цены</caption>'
            '<thead><tr><th>Дата · UTC</th><th>Цена</th><th>Изм.</th></tr></thead><tbody>'
            + history_rows(points)
            + "</tbody></table>"
            if points
            else '<p class="muted">Наблюдений в наличии пока нет.</p>'
        )

        # price vs target progress (for card)
        progress = ""
        if target and row["kind"] == "card":
            pct = max(0, min(100, (target / row["price"] * 100) if row["price"] else 0))
            # when reached, 100%
            if reached:
                pct = 100
            progress = f'<div class="progress" aria-hidden="true"><i style="width:{pct:.0f}%"></i></div>'

        cards.append(
            f'''<article class="product-card" data-kind="{_esc(row['kind'])}" data-shop="{_esc(row['shop'])}"
            data-chip="{_esc(row['chip'] or '')}" data-price="{row['price']}" data-pp="{pp or 1e12}"
            data-target="{int(reached)}" data-stock="{int(bool(row['in_stock']))}" data-fresh="{int(is_fresh)}"
            data-identity="{_esc(row['identity'])}" data-title="{_esc(row['title'].lower())}">
            {visual}<div class="card-body"><div class="meta"><span>{_esc(row['shop'])}</span><span>{label}</span></div>
            <h3>{_esc(row['title'])}</h3><p class="status s--{status_kind}">{status} · {row['memory_gb'] or '—'} ГБ VRAM</p>
            {badges_html}
            <p class="price">{_fmt(row['price'])} <span>₸</span></p>{progress}{target_text}{perf}{old}
            <div class="card-history"><h4>История <span>{min(5, len(points))} точек</span></h4>{hist}
            <p class="note">% к предыдущему наблюдению · новые сверху</p></div>
            <details><summary>Данные</summary><p>Наблюдение: {_esc(row['observed_at'][:16].replace('T', ' '))} UTC</p>
            <p>Партномер: {_esc(row['part_number'] or '—')}</p><p>Класс: {_esc(row['class_key'] or '—')}</p>
            <p>{'Та же модель' if row['part_number'] else 'Приблизительно по классу'}.</p></details>
            {link}</div></article>'''
        )

        json_items.append(
            {
                "identity": row["identity"],
                "shop": row["shop"],
                "kind": row["kind"],
                "title": row["title"],
                "price": row["price"],
                "url": url,
                "class_key": row["class_key"],
                "chip": row["chip"],
                "memory_gb": row["memory_gb"],
                "in_stock": bool(row["in_stock"]),
                "fresh": bool(is_fresh),
                "reached": bool(reached),
                "target": target,
                "pp": round(pp, 2) if pp else None,
                "ppr": round(ppr, 2) if ppr else None,
                "g3d": rating.g3d if rating else None,
                "rel_perf": bench.rel_perf if bench else None,
                "tier": bench.tier_label if bench else None,
                "tier_key": bench.tier_key if bench else None,
                "observed_at": row["observed_at"],
            }
        )

    # market panels
    market: list[str] = []
    for key in sorted({r["class_key"] for r in rows if r["kind"] == "card" and r["class_key"]}):
        points = _class_daily_medians(conn, key)
        low_row = conn.execute(
            """SELECT MIN(price) AS price FROM observations WHERE class_key = ? AND kind = 'card'
            AND in_stock = 1 AND price > 0 AND observed_at >= ?""",
            (key, since),
        ).fetchone()
        low = low_row[0] if low_row else None
        current = min(
            (r["price"] for r in rows
             if r["class_key"] == key and r["kind"] == "card" and r["in_stock"] and r["observed_at"] >= fresh),
            default=None,
        )
        tgt = targets.get(key)
        tgt_line = ""
        if current and tgt:
            if current <= tgt:
                tgt_line = f'<span class="mini good">цель {_fmt(tgt)} ✓</span>'
            else:
                tgt_line = f'<span class="mini">дороже цели на {_fmt(current - tgt)}</span>'
        spark = _svg_sparkline(points) or '<p class="muted">Недостаточно данных.</p>'
        count = class_counts.get(key, 0)
        market.append(
            f'<section class="panel market-card"><div class="market-head"><p class="eyebrow">{_esc(class_label(key))}</p>'
            f'<span class="count-badge" title="Предложений за 30 дней">{count}</span></div>'
            f'<h3>Лучшая цена сейчас</h3>'
            f'<p class="price">{_fmt(current) if current else "—"} <span>₸</span> {tgt_line}</p>'
            f'<p class="muted">Минимум за 30 дней: {_fmt(low) + "&nbsp;₸" if low else "—"} · линия — дневная медиана класса</p>{spark}</section>'
        )

    leader_rows = "".join(
        f'<tr><td>{_esc(r["title"])}</td><td>{_esc(shop_label(r["shop"]))}</td><td class="num">{_fmt(r["price"])}&nbsp;₸</td><td class="num">{f"{pp:.1f}".replace(".", ",")}</td></tr>'
        for pp, r in sorted(leaders, key=lambda pair: pair[0])[:10]
    )

    alerts = conn.execute(
        """SELECT a.alerted_at, a.alerted_price,
        (SELECT title FROM observations WHERE identity = a.identity ORDER BY observed_at DESC, id DESC LIMIT 1) AS title
        FROM alerts a ORDER BY alerted_at DESC LIMIT 10"""
    ).fetchall()
    alert_rows = "".join(
        f'<tr><td>{_esc(r["alerted_at"][:16].replace("T", " "))}</td>'
        f'<td>{_esc(r["title"] or "Товар отсутствует")}</td><td class="num">{_fmt(r["alerted_price"])}&nbsp;₸</td></tr>'
        for r in alerts
    )
    if alert_rows:
        alerts_section = (
            '<section id="alerts"><div class="section-heading"><div><p class="eyebrow">Сигналы</p><h2>Последние сигналы бота</h2>'
            '<p class="section-sub">Автоматические находки по медиане, тренду и целевым ценам.</p></div></div>'
            '<div class="panel table-wrap glass" tabindex="0" role="region" aria-label="Последние сигналы">'
            '<table><thead><tr><th>Дата · UTC</th><th>Предложение</th><th>Цена сигнала</th></tr></thead>'
            f"<tbody>{alert_rows}</tbody></table></div></section>"
        )
    else:
        alerts_section = ""

    # watchlist progress
    watch_html = ""
    if watch_order:
        # best price per watched class
        best_by_class: dict[str, tuple[int, str]] = {}
        for r in rows:
            ck = r["class_key"]
            if ck in targets and r["kind"] == "card" and r["in_stock"] and r["observed_at"] >= fresh:
                cur = best_by_class.get(ck)
                if cur is None or r["price"] < cur[0]:
                    best_by_class[ck] = (r["price"], r["shop"])
        chips_watch = []
        for ck in watch_order:
            tgt = targets.get(ck)
            best = best_by_class.get(ck)
            if not tgt:
                continue
            if best:
                price, shop = best
                pct = min(100, tgt / price * 100) if price else 0
                reached = price <= tgt
                chips_watch.append(
                    f'<div class="watch-item {"ok" if reached else ""}"><div class="watch-head"><strong>{_esc(class_label(ck))}</strong>'
                    f'<span>{_fmt(price)}&nbsp;₸ · {_esc(shop_label(shop))}</span></div>'
                    f'<div class="progress sm"><i style="width:{pct:.0f}%"></i></div>'
                    f'<span class="watch-meta">Цель {_fmt(tgt)} · {"достигнута" if reached else f"ещё {_fmt(price - tgt)}"}</span></div>'
                )
            else:
                chips_watch.append(
                    f'<div class="watch-item"><div class="watch-head"><strong>{_esc(ck)}</strong><span>нет данных</span></div>'
                    f'<span class="watch-meta">Цель {_fmt(tgt)}</span></div>'
                )
        if chips_watch:
            watch_html = '<div class="watch-grid">' + "".join(chips_watch) + "</div>"

    def options(values):
        return "".join(f'<option value="{_esc(v)}">{_esc(v)}</option>' for v in sorted(values))

    assets = Path(__file__).with_name("dashboard_assets")
    template = (assets / "page.html").read_text(encoding="utf-8")
    # JSON island
    json_blob = json.dumps(
        {"generated": now.isoformat(), "count": len(rows), "items": json_items},
        ensure_ascii=False,
    )
    values = {
        "css": (assets / "style.css").read_text(encoding="utf-8"),
        "js": (assets / "app.js").read_text(encoding="utf-8"),
        "cards": "".join(cards),
        "market": "".join(market),
        "leaders": leader_rows,
        "alerts_section": alerts_section,
        "watch": watch_html,
        "json": json_blob,
        "shops": options({r["shop"] for r in rows}),
        "chips": options({r["chip"] for r in rows if r["chip"]}),
        "count": str(len(rows)),
        "shop_count": str(len({r["shop"] for r in rows})),
        "target_count": str(sum('data-target="1"' in c for c in cards)),
        "generated": now.strftime("%d.%m.%Y %H:%M UTC"),
        "observed": max((r["observed_at"] for r in rows), default="Нет данных")[:16].replace("T", " "),
    }
    import re

    page = re.sub(r"@@(\w+)@@", lambda m: values.get(m.group(1), ""), template)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return len(market)
