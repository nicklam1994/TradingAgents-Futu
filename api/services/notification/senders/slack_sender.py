"""Slack webhook sender.

Sends messages to a Slack channel via an incoming webhook URL.
Ref: https://api.slack.com/messaging/webhooks
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


class SlackSender(NotificationSender):
    """Send messages via Slack incoming webhook.

    Config keys:
        - ``enabled`` (bool): whether this channel is active
        - ``webhook_url`` (str): Slack webhook URL
        - ``channel`` (str, optional): override target channel
        - ``username`` (str, optional): override bot username
        - ``icon_emoji`` (str, optional): override bot icon (e.g. ":chart_with_upwards_trend:")
    """

    channel = NotificationChannel.SLACK

    def send(self, message: str, config: Dict[str, Any]) -> bool:
        webhook_url = config.get("webhook_url", "")
        if not webhook_url:
            logger.warning("[slack_sender] no webhook_url configured")
            return False

        # Slack has no hard text limit but ~4000 chars is safe
        text = message[:4000]

        payload: Dict[str, Any] = {"text": text}
        if config.get("channel"):
            payload["channel"] = config["channel"]
        if config.get("username"):
            payload["username"] = config["username"]
        if config.get("icon_emoji"):
            payload["icon_emoji"] = config["icon_emoji"]

        try:
            resp = requests.post(
                webhook_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json;charset=utf-8"},
                timeout=10,
            )
            # Slack returns "ok" (200) on success
            if resp.status_code == 200 and resp.text == "ok":
                logger.info("[slack_sender] sent OK")
                return True
            logger.warning("[slack_sender] HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        except Exception as exc:
            logger.error("[slack_sender] send failed: %s", exc)
            return False
