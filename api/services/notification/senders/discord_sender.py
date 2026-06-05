"""Discord webhook sender.

Sends messages to a Discord channel via an incoming webhook.
Ref: https://discord.com/developers/docs/resources/webhook
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import requests

from api.services.notification.notification_channel import (
    NotificationChannel,
    NotificationSender,
)

logger = logging.getLogger(__name__)


class DiscordSender(NotificationSender):
    """Send messages via Discord webhook.

    Config keys:
        - ``enabled`` (bool): whether this channel is active
        - ``webhook_url`` (str): Discord webhook URL
        - ``username`` (str, optional): override webhook username
        - ``avatar_url`` (str, optional): override webhook avatar
    """

    channel = NotificationChannel.DISCORD

    def send(self, message: str, config: Dict[str, Any]) -> bool:
        webhook_url = config.get("webhook_url", "")
        if not webhook_url:
            logger.warning("[discord_sender] no webhook_url configured")
            return False

        # Discord has a 2000 char limit per message
        text = message[:2000]

        payload: Dict[str, Any] = {"content": text}
        if config.get("username"):
            payload["username"] = config["username"]
        if config.get("avatar_url"):
            payload["avatar_url"] = config["avatar_url"]

        try:
            resp = requests.post(
                webhook_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            # Discord returns 204 on success (no content)
            if resp.status_code in (200, 204):
                logger.info("[discord_sender] sent OK")
                return True
            logger.warning("[discord_sender] HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        except Exception as exc:
            logger.error("[discord_sender] send failed: %s", exc)
            return False
