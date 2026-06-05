"""飞书 (Feishu/Lark) webhook sender.

Sends messages to a Feishu group via its incoming webhook bot.
Ref: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import time
from typing import Any, Dict

import requests

from api.services.notification.notification_channel import (
    NotificationChannel,
    NotificationSender,
)

logger = logging.getLogger(__name__)


class FeishuSender(NotificationSender):
    """Send messages via 飞书 custom bot webhook.

    Config keys:
        - ``enabled`` (bool): whether this channel is active
        - ``webhook_url`` (str): Feishu webhook URL
        - ``secret`` (str, optional): signing secret for signature verification
    """

    channel = NotificationChannel.FEISHU

    def send(self, message: str, config: Dict[str, Any]) -> bool:
        webhook_url = config.get("webhook_url", "")
        if not webhook_url:
            logger.warning("[feishu_sender] no webhook_url configured")
            return False

        payload: Dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": message[:4000]},
        }

        # If a signing secret is provided, add timestamp + sign
        secret = config.get("secret", "")
        if secret:
            timestamp = str(int(time.time()))
            sign = self._gen_sign(timestamp, secret)
            payload["timestamp"] = timestamp
            payload["sign"] = sign

        try:
            resp = requests.post(
                webhook_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json;charset=utf-8"},
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            ok = body.get("code", -1) == 0 or body.get("StatusCode", -1) == 0
            if ok:
                logger.info("[feishu_sender] sent OK")
            else:
                logger.warning("[feishu_sender] API error: %s", body)
            return ok
        except Exception as exc:
            logger.error("[feishu_sender] send failed: %s", exc)
            return False

    @staticmethod
    def _gen_sign(timestamp: str, secret: str) -> str:
        """Generate Feishu webhook signature."""
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        return base64.b64encode(hmac_code).decode("utf-8")
