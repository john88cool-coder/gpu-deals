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
# Повторные попытки при 429/5xx/сетевых сбоях: ограниченные, с паузами.
_MAX_ATTEMPTS = 3


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
    """Канал доставки уведомлений.

    `buttons` — строки inline-кнопок с ссылками (текст, url); каналы без
    поддержки кнопок (консоль) их игнорируют. `confirms_delivery` — канал
    подтверждает доставку (Telegram — да, консоль — нет: dry-run не должен
    расходовать дедупликацию находок).
    """

    def send(
        self, text: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> None: ...


# Лимитер на уровне чата: пауза между ЛЮБЫМИ двумя запросами, а не только
# между частями одного сообщения — breakage, digest и heartbeat идут подряд.
_LAST_POST_TS = 0.0


class TelegramNotifier:
    """Отправка через Bot API. Длинные сообщения нарезаются автоматически;
    429 с retry_after и временные ошибки 5xx/сети повторяются ограниченно.

    confirms_delivery = True: успешный send подтверждает доставку находок.
    """

    confirms_delivery = True

    def __init__(self, token: str, chat_id: str, timeout: float = 20.0) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout

    def send(
        self, text: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> None:
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            if index:
                time.sleep(_SEND_INTERVAL_S)
            payload: dict = {
                "chat_id": self._chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            # Кнопки имеет смысл вешать только на последний кусок: текст мог
            # разрезаться, а ссылка относится к находке в его конце.
            if buttons and index == len(chunks) - 1:
                payload["reply_markup"] = {
                    "inline_keyboard": [
                        [{"text": label, "url": url} for label, url in row]
                        for row in buttons
                    ]
                }
            self._post(payload)

    def _post(self, payload: dict) -> None:
        global _LAST_POST_TS
        last_error: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            wait = _LAST_POST_TS + _SEND_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _LAST_POST_TS = time.monotonic()
            try:
                response = httpx.post(self._url, json=payload, timeout=self._timeout)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code == 429:
                    last_error = RuntimeError("429")
                    try:
                        retry_after = response.json()["parameters"]["retry_after"]
                    except Exception:  # noqa: BLE001 — нет retry_after в ответе
                        retry_after = 5
                    time.sleep(retry_after + 1)
                elif response.status_code >= 500 and attempt < _MAX_ATTEMPTS - 1:
                    last_error = RuntimeError(f"{response.status_code}")
                    time.sleep(3)
                else:
                    response.raise_for_status()
                    return
        raise last_error if last_error else RuntimeError("отправка не удалась")


class ConsoleNotifier:
    """Вывод в stdout — для отладки без токена. Доставку не подтверждает."""

    confirms_delivery = False

    def send(
        self, text: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> None:
        print(text)
        if buttons:
            for row in buttons:
                for label, url in row:
                    print(f"[кнопка] {label} -> {url}")
