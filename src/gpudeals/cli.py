"""Точка входа: gpu-deals crawl | heartbeat | dry-run."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import settings
from .crawler import run_once, send_digest, send_heartbeat
from .notify import ConsoleNotifier, Notifier, TelegramNotifier
from .shops import REGISTRY


def _refresh_benchmarks() -> None:
    """Обновляет справочник рейтинга PassMark и печатает итог."""
    import logging

    from . import benchmarks

    total = benchmarks.refresh()
    logging.info("справочник PassMark обновлён, записей: %s", total)


def _build_notifier(force_console: bool) -> Notifier:
    if force_console or not (settings.telegram_token and settings.telegram_chat_id):
        if not force_console:
            print(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — вывод в консоль",
                file=sys.stderr,
            )
        return ConsoleNotifier()
    return TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gpu-deals", description="Мониторинг скидок на видеокарты в магазинах Казахстана"
    )
    parser.add_argument(
        "command",
        choices=("crawl", "watchlist", "heartbeat", "digest", "refresh-benchmarks"),
        help=(
            "crawl — полный обход; watchlist — быстрая проверка избранных моделей; "
            "heartbeat — строка о живости по итогам последних обходов; "
            "digest — недельный дайджест рынка; "
            "refresh-benchmarks — обновить справочник рейтинга PassMark"
        ),
    )
    parser.add_argument("--shop", action="append", choices=sorted(REGISTRY), help="только эти магазины")
    parser.add_argument("--console", action="store_true", help="печатать вместо отправки в Telegram")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    notifier = _build_notifier(args.console)

    # Справочник рейтинга обновляется без обхода магазинов и без Telegram.
    if args.command == "refresh-benchmarks":
        _refresh_benchmarks()
        return 0

    # Сводка и дайджест читаются из базы: собственного обхода не делают.
    if args.command == "heartbeat":
        send_heartbeat(notifier, shops=args.shop)
        return 0
    if args.command == "digest":
        send_digest(notifier)
        return 0

    count = run_once(notifier, shops=args.shop, watchlist_only=args.command == "watchlist")
    if count == 0:
        logging.info("находок нет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
