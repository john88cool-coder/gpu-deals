"""Тесты нарезки длинных сообщений под лимит Telegram."""

from __future__ import annotations

from gpudeals.notify import TELEGRAM_MAX_CHARS, split_message


def test_short_text_untouched() -> None:
    assert split_message("короткое сообщение") == ["короткое сообщение"]


def test_long_text_splits_by_lines() -> None:
    lines = [f"строка {i} с текстом" for i in range(400)]
    text = "\n".join(lines)
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_MAX_CHARS for chunk in chunks)
    # Ничего не потеряно и порядок сохранён.
    assert "\n".join(chunks).replace("\n\n", "\n") == text.replace("\n\n", "\n") or (
        "".join(chunk + "\n" for chunk in chunks).strip() == text
    )


def test_split_always_on_line_boundary() -> None:
    text = "\n".join(f"предложение номер {i}" * 3 for i in range(300))
    for chunk in split_message(text):
        # Кусок не обрывает строку посередине: начинается не с середины слова.
        assert chunk == "" or not chunk[0].islower() or chunk.startswith("предложение")


def test_huge_line_without_newlines_is_cut_hard() -> None:
    text = "а" * 9000
    chunks = split_message(text)
    assert len(chunks) == 3
    assert all(len(chunk) <= TELEGRAM_MAX_CHARS for chunk in chunks)


def test_cold_start_digest_fits_after_split() -> None:
    """Реалистичный дайджест холодного старта (~380 находок) должен нарезаться."""
    offer = (
        "<b>Видеокарта Gigabyte RTX 5070 WINDFORCE OC 12GB 192bit/G7 (HDMI+3DP)"
        "[GV-N5070WF3OC-12GD]</b>\nЦена: 457 990 ₸\n"
        "• новая позиция в бюджете (≤ 600 000 ₸)\n"
        "Магазин указывает: 555 990 ₸ → 457 990 ₸ (не проверено, справочно)\n"
        '<a href="https://www.technodom.kz/p/test-offer-289980">technodom</a>'
    )
    digest = f"🎯 Находок: 380\n\n" + "\n\n".join([offer] * 380)
    assert len(digest) > 100_000
    chunks = split_message(digest)
    assert len(chunks) >= 25
    assert all(len(chunk) <= TELEGRAM_MAX_CHARS for chunk in chunks)
