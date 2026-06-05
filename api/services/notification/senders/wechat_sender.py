"""企业微信 (WeCom) webhook sender.

Integrates with the existing ``wecom_notification_service`` for URL
validation and message delivery.
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


class WechatSender(NotificationSender):
    """Send messages via 企业微信 group robot webhook.

    Config keys:
        - ``enabled`` (bool): whether this channel is active
        - ``webhook_url`` (str): full webhook URL (or just the key)
    """

    channel = NotificationChannel.WECHAT

    def send(self, message: str, config: Dict[str, Any]) -> bool:
        from api.services.wecom_notification_service import normalize_webhook_url

        webhook_url = config.get("webhook_url", "")
        if not webhook_url:
            logger.warning("[wechat_sender] no webhook_url configured")
            return False

        try:
            url = normalize_webhook_url(webhook_url)
        except ValueError as exc:
            logger.error("[wechat_sender] invalid webhook URL: %s", exc)
            return False

        payload = {"msgtype": "text", "text": {"content": message[:1800]}}
        try:
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json;charset=utf-8"},
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            ok = int(body.get("errcode", -1)) == 0
            if ok:
                logger.info("[wechat_sender] sent OK")
            else:
                logger.warning("[wechat_sender] API error: %s", body)
            return ok
        except Exception as exc:
            logger.error("[wechat_sender] send failed: %s", exc)
            return False
