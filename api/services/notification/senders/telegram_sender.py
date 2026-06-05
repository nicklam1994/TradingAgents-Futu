"""Telegram Bot API sender.

Sends messages via the Telegram Bot API (sendMessage endpoint).
Ref: https://core.telegram.org/bots/api#sendmessage
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import requests

from api.services.notification.notification_channel import (
    NotificationChannel,
    NotificationSender,
)

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"


class TelegramSender(NotificationSender):
    """Send messages via Telegram Bot API.

    Config keys:
        - ``enabled`` (bool): whether this channel is active
        - ``bot_token`` (str): Telegram bot token (from @BotFather)
        - ``chat_id`` (str): target chat ID (user, group, or channel)
        - ``parse_mode`` (str, optional): "MarkdownV2", "HTML", or "" (plain)
    """

    channel = NotificationChannel.TELEGRAM

    def send(self, message: str, config: Dict[str, Any]) -> bool:
        bot_token = config.get("bot_token", "")
        chat_id = config.get("chat_id", "")
        if not bot_token or not chat_id:
            logger.warning("[telegram_sender] missing bot_token or chat_id")
            return False

        url = f"{_TELEGRAM_API}/bot{bot_token}/sendMessage"
        # Telegram has a 4096 char limit per message
        text = message[:4096]

        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        # Don't set parse_mode for plain text to avoid Markdown parsing errors
        parse_mode = config.get("parse_mode", "")
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            ok = body.get("ok", False)
            if ok:
                logger.info("[telegram_sender] sent OK to chat %s", chat_id)
            else:
                logger.warning("[telegram_sender] API error: %s", body)
            return ok
        except Exception as exc:
            logger.error("[telegram_sender] send failed: %s", exc)
            return False
