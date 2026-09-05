"""Тесты формата уведомлений."""

from __future__ import annotations

from gpudeals.evaluate import Signal, Verdict
from gpudeals.models import ItemKind, Offer
from gpudeals.report import format_breakage, format_digest, format_heartbeat, format_offer


def card(**overrides) -> Offer:
    base = dict(
        shop="technodom",
        kind=ItemKind.CARD,
        title="Видеокарта Gigabyte RTX 5070 WINDFORCE OC 12GB",
        price=457_990,
        url="https://www.technodom.kz/p/test",
        class_key="rtx5070-12",
        part_number="GV-N5070WF3OC-12GD",
        chip="rtx5070",
        memory_gb=12,
    )
    return Offer(**{**base, **overrides})


def test_shop_discount_is_marked_unverified() -> None:
    verdict = Verdict(
        offer=card(shop_old_price=555_990, shop_discount_pct=18),
        signals=[(Signal.NEW_IN_BUDGET, "новая позиция в бюджете")],
    )
    text = format_offer(verdict)
    assert "не проверено, справочно" in text


def test_prices_are_formatted_with_spaces() -> None:
    text = format_offer(Verdict(offer=card(), signals=[(Signal.NEW_IN_BUDGET, "тест")]))
    assert "457 990 ₸" in text


def test_perf_line_hidden_when_below_class_signal_present() -> None:
    """Внутри класса чип один и тот же, поэтому две строки повторяли бы друг друга."""
    verdict = Verdict(
        offer=card(price=410_990),
        signals=[(Signal.BELOW_CLASS, "на 13% дешевле медианы класса (470 486 ₸)")],
        class_median=470_486,
        perf_vs_class_pct=12.6,
    )
    text = format_offer(verdict)
    assert "дешевле медианы класса" in text
    assert "Цена за производительность" not in text


def test_perf_line_hidden_when_difference_is_noise() -> None:
    verdict = Verdict(
        offer=card(),
        signals=[(Signal.NEW_IN_BUDGET, "тест")],
        class_median=470_990,
        perf_vs_class_pct=1.4,
    )
    assert "Цена за производительность" not in format_offer(verdict)


def test_approximate_match_note_only_for_cards() -> None:
    no_part_number = card(part_number=None)
    assert "приблизительное" in format_offer(
        Verdict(offer=no_part_number, signals=[(Signal.NEW_IN_BUDGET, "тест")])
    )

    build = card(
        kind=ItemKind.BUILD,
        title="IT-MR i5-14400F / RTX 5070 12 Гб / 32 Гб / SSD 1000 Гб",
        price=870_000,
        part_number=None,
    )
    assert "приблизительное" not in format_offer(
        Verdict(offer=build, signals=[(Signal.NEW_IN_BUDGET, "тест")])
    )


def test_build_residual_explains_what_is_included() -> None:
    build = card(kind=ItemKind.BUILD, part_number=None, price=870_000)
    verdict = Verdict(
        offer=build, signals=[(Signal.NEW_IN_BUDGET, "тест")], build_residual=412_010
    )
    text = format_offer(verdict)
    assert "412 010 ₸" in text
    assert "процессор" in text


def test_over_budget_is_flagged_not_silenced() -> None:
    verdict = Verdict(offer=card(price=757_990), signals=[], over_budget_by=157_990)
    assert "Выше бюджета на 157 990 ₸" in format_offer(verdict)


def test_digest_separates_cards_and_builds() -> None:
    cards = Verdict(offer=card(), signals=[(Signal.NEW_IN_BUDGET, "тест")])
    build = Verdict(
        offer=card(kind=ItemKind.BUILD, part_number=None),
        signals=[(Signal.NEW_IN_BUDGET, "тест")],
    )
    text = format_digest([cards, build])
    assert "Находок: 2" in text
    assert "Видеокарты" in text
    assert "Готовые сборки" in text


def test_breakage_message_names_shop_and_previous_count() -> None:
    text = format_breakage("technodom", 58)
    assert "technodom" in text
    assert "58" in text


def test_heartbeat_counts_alive_shops() -> None:
    text = format_heartbeat([("technodom", 81, True), ("kaspi", 49, True), ("dns", 0, False)])
    assert "2/3" in text
    assert "130" in text
    assert "kaspi: 49" in text
    assert "dns: ошибка" in text


def test_shop_text_is_escaped_for_html_mode() -> None:
    """Сообщения уходят с parse_mode=HTML: `&` в названии — это ответ 400 от
    Bot API, упавший шаг обхода и потерянный снимок цен за этот обход."""
    verdict = Verdict(
        offer=card(
            title="Видеокарта ASUS <ROG> Strix & TUF 16GB",
            url="https://shop.kz/offer/x?a=1&b=2",
            stock_note="под заказ <7 дней>",
            part_number=None,
        ),
        signals=[(Signal.NEW_IN_BUDGET, "тест")],
    )
    text = format_offer(verdict)

    assert "&lt;ROG&gt;" in text
    assert "Strix &amp; TUF" in text
    assert "под заказ &lt;7 дней&gt;" in text
    # Ссылка остаётся разметкой, но её содержимое экранировано.
    assert '<a href="https://shop.kz/offer/x?a=1&amp;b=2">technodom</a>' in text
    assert text.startswith("<b>")
    # Ни одного необработанного угла из данных магазина.
    assert "<ROG>" not in text

