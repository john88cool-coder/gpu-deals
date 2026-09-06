"""Тесты справочника рейтинга PassMark.

Разметка страниц рейтинга воспроизведена по живым страницам
videocardbenchmark.net: запись — ссылка gpu.php с именем и id товара, за ней
span.count с баллом; каждая модель встречается в разметке дважды.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gpudeals import benchmarks
from gpudeals.config import settings
from gpudeals.models import ItemKind, Offer

# Порядок следования и есть рейтинг: первая запись — первое место.
# Китайские D-варианты включены, чтобы проверить фильтр: чипом они не отличимы
# от десктопной версии и ломали бы откат по чипу.
_PAGE = """
<li id="rk5940">
<a href="gpu.php?gpu=GeForce+RTX+5090&amp;id=5940">
<span class="prdname" >GeForce RTX 5090</span>
<span class="count">38,986</span>
<a href="gpu.php?gpu=GeForce+RTX+5090+D&amp;id=6001">
<span class="prdname" >GeForce RTX 5090 D</span>
<span class="count">42,042</span>
<a href="gpu.php?gpu=GeForce+RTX+5090+D+v2&amp;id=6002">
<span class="prdname" >GeForce RTX 5090 D v2</span>
<span class="count">34,728</span>
<a href="gpu.php?gpu=GeForce+RTX+5080&amp;id=5880">
<span class="prdname" >GeForce RTX 5080</span>
<span class="count">35,622</span>
<a href="gpu.php?gpu=GeForce+RTX+5070+Ti+Laptop+GPU&amp;id=6216">
<span class="prdname" >GeForce RTX 5070 Ti Laptop GPU</span>
<span class="count">31,000</span>
<a href="gpu.php?gpu=GeForce+RTX+5060+Ti+16GB&amp;id=5910">
<span class="prdname" >GeForce RTX 5060 Ti 16GB</span>
<span class="count">22,616</span>
<a href="gpu.php?gpu=RTX+5000+Ada+Generation&amp;id=5200">
<span class="prdname" >RTX 5000 Ada Generation</span>
<span class="count">30,000</span>
<a href="gpu.php?gpu=Quadro+RTX+5000&amp;id=4300">
<span class="prdname" >Quadro RTX 5000</span>
<span class="count">15,302</span>
<a href="gpu.php?gpu=GeForce+RTX+2070+SUPER&amp;id=4116">
<span class="prdname" >GeForce RTX 2070 SUPER</span>
<span class="count">18,122</span>
<a href="gpu.php?gpu=Unknown+Thing&amp;id=1">
<span class="prdname" >Unknown Thing</span>
<span class="count">9,000</span>
"""


@pytest.fixture
def reference(tmp_path, monkeypatch):
    """Справочник из синтетической страницы вместо живого CSV."""
    csv_path = tmp_path / "gpu_benchmarks.csv"
    ratings = benchmarks.parse_rating_pages([_PAGE, ""])
    benchmarks.write_csv(ratings, csv_path)
    # benchmarks импортирует константу из config, патчить нужно её копию.
    monkeypatch.setattr("gpudeals.benchmarks.BENCHMARKS_CSV", csv_path)
    benchmarks._ratings.cache_clear()
    yield csv_path
    benchmarks._ratings.cache_clear()


def test_parse_keeps_order_and_drops_laptops() -> None:
    ratings = benchmarks.parse_rating_pages([_PAGE])

    names = [r.model_name for r in ratings]
    # Ноутбучная запись выброшена: иначе она столкнулась бы с десктопной
    # RTX 5070 Ti в одном class_key. Китайские D-варианты — тоже: их чип
    # неразличим с десктопным RTX 5090. Мусор без чипа — тоже.
    assert names == [
        "GeForce RTX 5090",
        "GeForce RTX 5080",
        "GeForce RTX 5060 Ti 16GB",
        "RTX 5000 Ada Generation",
        "Quadro RTX 5000",
        "GeForce RTX 2070 SUPER",
    ]
    # Порядок следования = рейтинг.
    assert [r.rank for r in ratings] == [1, 2, 3, 4, 5, 6]


def test_parse_is_stable_against_duplicate_markup() -> None:
    """Каждая модель встречается в разметке дважды — учитывается один раз."""
    page = _PAGE + _PAGE
    ratings = benchmarks.parse_rating_pages([page])
    assert len({r.model_name for r in ratings}) == len(ratings)


def test_class_key_refers_to_memory_variant(reference) -> None:
    """«GeForce RTX 5060 Ti 16GB» — отдельная позиция рейтинга с памятью."""
    assert benchmarks.rating_for("rtx5060ti-16", None) is not None


def test_chip_fallback_works_only_when_unique(reference) -> None:
    """Откат по чипу запрещён, если записей чипа несколько.

    «RTX 5000 Ada» и «Quadro RTX 5000» — один чип, разные карты. А RTX 5090
    после отбрасывания D-вариантов остаётся в единственном числе.
    """
    # RTX 5080: у чипа одна запись — откат срабатывает (в имени нет памяти).
    assert benchmarks.rating_for("rtx5080-16", "rtx5080").g3d == 35_622
    # RTX 5090: D-варианты отфильтрованы, запись одна.
    assert benchmarks.rating_for("rtx5090-32", "rtx5090").g3d == 38_986
    # RTX 5000: рабочие варианты неразличимы — угадывать нельзя.
    assert benchmarks.rating_for("rtx5000-32", "rtx5000") is None


def test_missing_chip_has_no_rating(reference) -> None:
    assert benchmarks.format_rating("rx9999-99", "rx9999") is None


def test_owner_comparison_faster_and_slower(reference) -> None:
    """RTX 5080 против RTX 2070 SUPER (18 122): в ~2 раза быстрее."""
    line = benchmarks.format_rating("rtx5080-16", "rtx5080")
    assert "в 2,0 раза быстрее вашей RTX 2070 SUPER" in line

    slow = benchmarks.Rating(chip="rtx3050", model_name="GeForce RTX 3050 8GB",
                             g3d=8_523, rank=99, class_key="rtx3050-8")
    assert "медленнее вашей RTX 2070 SUPER" in benchmarks._owner_comparison(slow.g3d, benchmarks._owner_rating())


def test_owner_comparison_near_par(reference, monkeypatch) -> None:
    """Карта на уровне текущей не выдаёт бессмысленное «в 1,0 раза быстрее»."""

    par = benchmarks.Rating(chip="rtxxxx", model_name="X", g3d=18_300, rank=99, class_key=None)
    text = benchmarks._owner_comparison(par.g3d, benchmarks._owner_rating())
    assert "на уровне вашей RTX 2070 SUPER" in text


def test_format_rating_line_shape(reference) -> None:
    line = benchmarks.format_rating("rtx5060ti-16", "rtx5060ti")
    assert line.startswith("Балл PassMark: 22 616 (3-е из 6 десктопных RTX/RX)")


def test_report_shows_rating_for_known_card(reference) -> None:
    from gpudeals.evaluate import Signal, Verdict
    from gpudeals.report import format_offer

    offer = Offer(
        shop="technodom",
        kind=ItemKind.CARD,
        title="Видеокарта Gigabyte RTX 5080",
        price=700_000,
        url="https://example.kz",
        class_key="rtx5080-16",
        chip="rtx5080",
        memory_gb=16,
    )
    text = format_offer(Verdict(offer=offer, signals=[(Signal.NEW_IN_BUDGET, "тест")]))
    assert "Балл PassMark: 35 622" in text
    assert "быстрее вашей RTX 2070 SUPER" in text


def test_report_hides_rating_for_unknown_card(reference) -> None:
    from gpudeals.evaluate import Signal, Verdict
    from gpudeals.report import format_offer

    offer = Offer(
        shop="technodom",
        kind=ItemKind.CARD,
        title="Видеокарта Noname 9000",
        price=100_000,
        url="https://example.kz",
        class_key="rx9999-99",
        chip="rx9999",
        memory_gb=16,
    )
    text = format_offer(Verdict(offer=offer, signals=[(Signal.NEW_IN_BUDGET, "тест")]))
    assert "PassMark" not in text


def test_owner_gpu_configured() -> None:
    assert settings.owner_gpu_class_key == "rtx2070super"
    assert settings.owner_gpu_name == "RTX 2070 SUPER"
