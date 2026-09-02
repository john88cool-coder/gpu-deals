"""Отправка уведомлений. Интерфейс абстрактный, реализация одна — Telegram.

WhatsApp сознательно не реализован: вне 24-часового окна обслуживания он требует
модерируемых Meta платных шаблонов, а Казахстан с 1 октября 2026 переводится на
отдельный повышенный тариф. Неофициальные библиотеки (Baileys, whatsapp-web.js)
нарушают ToS и рискуют блокировкой личного номера.
"""

from __future__ import annotations

import time
from typing import Protocol

import httpx

# Telegram принимает сообщения не длиннее 4096 символов; запас — на
# HTML-разметку, которая считается в тот же лимит.
TELEGRAM_MAX_CHARS = 4000
# Лимит Bot API — 1 сообщение в секунду на чат.
_SEND_INTERVAL_S = 1.1


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Режет длинный текст на части по границам строк.

    Первый обход холодного старта даёт сотни находок — дайджест в разы длиннее
    лимита, и без нарезки отправка падала бы с ошибкой API.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class Notifier(Protocol):
    """Канал доставки уведомлений."""

    def send(self, text: str) -> None: ...


class TelegramNotifier:
    """Отправка через Bot API. Длинные сообщения нарезаются автоматически."""

    def __init__(self, token: str, chat_id: str, timeout: float = 20.0) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout

    def send(self, text: str) -> None:
        for index, chunk in enumerate(split_message(text)):
            if index:
                time.sleep(_SEND_INTERVAL_S)
            response = httpx.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()


class ConsoleNotifier:
    """Вывод в stdout — для отладки без токена."""

    def send(self, text: str) -> None:
        print(text)
