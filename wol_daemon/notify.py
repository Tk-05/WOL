from __future__ import annotations

import logging

import requests

from .config import NotificationConfig

logger = logging.getLogger("wol_daemon")


def send_notification(config: NotificationConfig, message: str) -> None:
    if config.ntfy is not None:
        try:
            requests.post(config.ntfy.url, data=message.encode("utf-8"), timeout=10)
        except requests.RequestException:
            logger.exception("ntfy notification failed")

    if config.telegram is not None:
        url = f"https://api.telegram.org/bot{config.telegram.bot_token}/sendMessage"
        try:
            requests.post(url, data={"chat_id": config.telegram.chat_id, "text": message}, timeout=10)
        except requests.RequestException:
            logger.exception("Telegram notification failed")
