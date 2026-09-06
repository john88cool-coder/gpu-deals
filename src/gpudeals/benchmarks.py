"""Справочник производительности GPU: рейтинг PassMark G3D.

Источник — открытые страницы рейтинга videocardbenchmark.net
(`high_end_gpus.html` + `mid_range_gpus.html`): на каждой серверной разметкой
лежит название, балл и доля от лидера, а порядок следования и есть рейтинг.
Справочник обновляется командой `gpu-deals refresh-benchmarks` (workflow
benchmarks.yml раз в месяц, два запроса) и коммитится в репозиторий.

Бот в момент отправки уведомления в сеть не ходит: сетевой запрос в горячем
пути — лишняя точка отказа, а баллы меняются раз в месяцы. Если PassMark
недоступен, остаётся прошлый коммит справочника, и уведомления этого не
замечают.

Строки в справочнике — это модели PassMark, нормализованные теми же
функциями, что и названия магазинов: `class_key` (чип + объём памяти) и `chip`.
Уточнение по памяти важно там, где PassMark различает варианты
(«GeForce RTX 5060 Ti 8GB» и «16GB» — разные позиции рейтинга).
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from .config import BENCHMARKS_CSV, settings
from .normalize import class_key, extract_chip, extract_memory_gb

log = logging.getLogger(__name__)

# Открытые страницы полного рейтинга: high_end — лидеры, mid_range продолжает.
RATING_PAGES = (
    "https://www.videocardbenchmark.net/high_end_gpus.html",
    "https://www.videocardbenchmark.net/mid_range_gpus.html",
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Одна запись рейтинга: ссылка с именем и id товара, за ней балл в span.count.
# Записи встречаются в разметке дважды (основной список и всплывающая карточка),
# поэтому при разборе оставляем первое вхождение id товара.
_ENTRY = re.compile(
    r'href="gpu\.php\?gpu=(?P<name>[^"]+)&amp;id=(?P<pid>\d+)".{0,500}?'
    r'span class="count">(?P<score>[\d,]+)',
    re.S,
)

# Ноутбучные и мобильные варианты: бот видит только десктопные карты. Без
# фильтра «RTX 5070 Ti Laptop GPU» столкнулся бы с десктопной записью класса,
# а «RTX 2070 with Max-Q Design» — с картой владельца в откате по чипу.
# Китайские D-варианты («RTX 5090 D», «RTX 5090 D v2») чипом не отличимы от
# десктопной версии и ломали бы уникальность записи RTX 5090.
_NOT_DESKTOP = re.compile(r"laptop|mobile|max-q|notebook|\sD(\sv\d+)?$", re.I)

# Здоровье разметки: в живом рейтинге полторы тысячи записей на страницу.
# Если разметка изменилась, эти маркеры пропадут раньше, чем успеет испортиться
# справочник.
_MIN_ENTRIES_PER_PAGE = 200


@dataclass(frozen=True)
class Rating:
    """Строка справочника: одна модель PassMark."""

    chip: str
    model_name: str
    g3d: int
    rank: int  # место среди десктопных моделей справочника, начиная с 1
    class_key: str | None


def _normalize_name(name: str) -> str:
    return name.replace("+", " ").strip()


def parse_rating_pages(pages: list[str]) -> list[Rating]:
    """Разбирает страницы рейтинга в порядок следования, то есть по местам.

    Ноутбучные варианты отбрасываются: иначе «RTX 5070 Ti Laptop GPU»
    нормализовался бы в тот же class_key, что и десктопная карта, и в справочнике
    оказалось бы два претендента на один класс.
    """
    ratings: list[Rating] = []
    seen_ids: set[str] = set()
    rank = 0
    for page in pages:
        for match in _ENTRY.finditer(page):
            pid = match.group("pid")
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            model_name = _normalize_name(match.group("name"))
            if _NOT_DESKTOP.search(model_name):
                continue
            if not (chip := extract_chip(model_name)):
                continue
            score = int(match.group("score").replace(",", ""))
            if score <= 0:
                continue

            rank += 1
            memory = extract_memory_gb(model_name, chip)
            ratings.append(
                Rating(
                    chip=chip,
                    model_name=model_name,
                    g3d=score,
                    rank=rank,
                    class_key=class_key(chip, memory),
                )
            )
    return ratings


def write_csv(ratings: list[Rating], path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["class_key", "chip", "model_name", "passmark_g3d", "desktop_rank"])
        for rating in ratings:
            writer.writerow([
                rating.class_key or "", rating.chip, rating.model_name,
                rating.g3d, rating.rank,
            ])
    tmp.replace(path)


def refresh(path: Path | None = None, client: httpx.Client | None = None) -> int:
    """Скачивает рейтинг, обновляет CSV. Возвращает число записей.

    В справочник попадают только модели, которые бот вообще может увидеть
    (RTX/RX по правилам normalize), поэтому итоговых строк ~полторы сотни,
    хотя на страницах рейтинга их полторы тысячи.
    """
    target = path or BENCHMARKS_CSV
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": _USER_AGENT}, timeout=60.0, follow_redirects=True
    )
    try:
        pages = []
        for url in RATING_PAGES:
            response = client.get(url)
            response.raise_for_status()
            html = response.text
            raw = len(re.findall(r'href="gpu\.php\?gpu=', html))
            if raw < _MIN_ENTRIES_PER_PAGE:
                raise RuntimeError(
                    f"на {url} распознано {raw} записей рейтинга вместо ожидаемых "
                    f">= {_MIN_ENTRIES_PER_PAGE} — изменилась разметка, "
                    "справочник не обновлён"
                )
            pages.append(html)
    finally:
        if own_client:
            client.close()

    ratings = parse_rating_pages(pages)
    if len(ratings) < 80:
        raise RuntimeError(
            f"PassMark вернул подозрительно мало моделей ({len(ratings)}), "
            "справочник не обновлён"
        )
    write_csv(ratings, target)
    return len(ratings)


@lru_cache(maxsize=1)
def _ratings() -> tuple[Rating, ...]:
    """Все записи справочника; файл читается один раз за процесс.

    Сломанный или старый справочник — не повод терять уведомление о находке:
    строка рейтинга лишь украшение, поэтому при ошибке чтения считаем
    справочник пустым.
    """
    if not BENCHMARKS_CSV.exists():
        return ()
    try:
        with BENCHMARKS_CSV.open(encoding="utf-8") as handle:
            return tuple(
                Rating(
                    chip=row["chip"],
                    model_name=row["model_name"],
                    g3d=int(row["passmark_g3d"]),
                    rank=int(row["desktop_rank"]),
                    class_key=row["class_key"] or None,
                )
                for row in csv.DictReader(handle)
                if row.get("chip") and row.get("passmark_g3d")
            )
    except (OSError, KeyError, ValueError, csv.Error) as exc:
        log.warning("справочник PassMark не читается (%s), строка рейтинга не показывается", exc)
        return ()


def rating_for(class_key: str | None, chip: str | None) -> Rating | None:
    """Рейтинг модели: точный класс, иначе единственная запись чипа.

    Откат по чипу работает только когда у чипа одна запись: у «RTX 5090» и
    «RTX 5090 D» чип одинаков, а записи разные, — угадывать между ними нельзя.
    """
    entries = _ratings()
    if class_key:
        for rating in entries:
            if rating.class_key == class_key:
                return rating
    if chip:
        matches = [r for r in entries if r.chip == chip]
        if len(matches) == 1:
            return matches[0]
    return None


def scores() -> dict[str, int]:
    """Баллы G3D по чипу — лучшая запись чипа. Для относительной оценки."""
    best: dict[str, Rating] = {}
    for rating in _ratings():
        current = best.get(rating.chip)
        if current is None or rating.rank < current.rank:
            best[rating.chip] = rating
    return {chip: rating.g3d for chip, rating in best.items()}


def price_per_point(chip: str | None, price: int) -> float | None:
    """Тенге за балл производительности. Меньше — лучше."""
    if not chip:
        return None
    score = scores().get(chip)
    return price / score if score else None


def relative_value_pct(chip: str | None, price: int, class_median: int | None) -> float | None:
    """Насколько цена за производительность лучше медианы класса, в процентах.

    Внутри одного класса чип одинаков, поэтому отношение баллов сокращается и
    величина сводится к разнице цен. Функция всё равно проходит через баллы:
    когда добавится сравнение между классами, формула не изменится.
    """
    if class_median is None:
        return None
    own = price_per_point(chip, price)
    reference = price_per_point(chip, class_median)
    if not own or not reference:
        return None
    return (reference - own) / reference * 100


def _owner_rating() -> Rating | None:
    """Карта владельца: обычно это чип без памяти в названии (RTX 2070), поэтому
    после точного класса срабатывает откат по чипу."""
    key = settings.owner_gpu_class_key
    return rating_for(key, key)


def _owner_comparison(score: int, owner: Rating) -> str | None:
    """Насколько карта быстрее или медленнее текущей карты владельца."""
    ratio = score / owner.g3d
    name = settings.owner_gpu_name
    if ratio >= 1.15:
        return f"в {ratio:.1f}".replace(".", ",") + f" раза быстрее вашей {name}"
    if ratio >= 0.85:
        return f"примерно на уровне вашей {name}"
    return f"на {abs(1 - ratio) * 100:.0f}% медленнее вашей {name}"


def format_rating(class_key: str | None, chip: str | None) -> str | None:
    """Строка рейтинга для уведомления, если модель есть в справочнике.

    Справочник — только десктопные RTX/RX, которые бот вообще видит, поэтому
    «место из N» — это место среди них, а не среди трёх тысяч Quadro и GTX.
    """
    rating = rating_for(class_key, chip)
    if rating is None:
        return None
    total = len(_ratings())
    line = (
        f"Балл PassMark: {_fmt_int(rating.g3d)} "
        f"({rating.rank}-е из {total} десктопных RTX/RX)"
    )
    owner = _owner_rating()
    if owner is not None:
        line += f", {_owner_comparison(rating.g3d, owner)}"
    return line


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")
