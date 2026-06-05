"""通用 Webhook sender (含钉钉).

Sends messages via a generic HTTP POST webhook.  Supports custom
headers, payload templates, and response validation.

This sender covers 钉钉 (DingTalk) and any other webhook-based
notification service that accepts a JSON POST body.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import requests

from api.services.notification.notification_channel import (
    NotificationChannel,
    NotificationSender,
)

logger = logging.getLogger(__name__)


class CustomWebhookSender(NotificationSender):
    """Send messages via a generic HTTP POST webhook.

    Config keys:
        - ``enabled`` (bool): whether this channel is active
        - ``webhook_url`` (str): target webhook URL
        - ``method`` (str, optional): HTTP method, default "POST"
        - ``headers`` (dict, optional): additional HTTP headers
        - ``content_type`` (str, optional): Content-Type header, default "application/json"
        - ``payload_key`` (str, optional): JSON key for the message body.
          Default "text".  For DingTalk, use "content.msg".
        - ``success_check`` (str, optional): dot-path to check in response JSON.
          E.g. "errcode == 0" for DingTalk.
        - ``timeout`` (int, optional): request timeout in seconds, default 10.

    DingTalk example config::

        {
            "enabled": true,
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=...",
            "payload_key": "content.msg",
            "success_check": "errcode == 0"
        }
    """

    channel = NotificationChannel.CUSTOM_WEBHOOK

    def send(self, message: str, config: Dict[str, Any]) -> bool:
        webhook_url = config.get("webhook_url", "")
        if not webhook_url:
            logger.warning("[custom_webhook] no webhook_url configured")
            return False

        method = config.get("method", "POST").upper()
        content_type = config.get("content_type", "application/json")
        timeout = config.get("timeout", 10)

        # Build payload based on payload_key
        payload_key = config.get("payload_key", "text")
        payload = self._build_payload(message, payload_key)

        # Merge custom headers
        headers: Dict[str, str] = {"Content-Type": content_type}
        custom_headers = config.get("headers", {})
        if isinstance(custom_headers, dict):
            headers.update(custom_headers)

        try:
            resp = requests.request(
                method,
                webhook_url,
                data=json.dumps(payload) if content_type.startswith("application/json") else payload,
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()

            # Validate response if success_check is configured
            success_check = config.get("success_check", "")
            if success_check:
                return self._check_response(resp, success_check)

            # Default: 2xx is success
            ok = 200 <= resp.status_code < 300
            if ok:
                logger.info("[custom_webhook] sent OK (HTTP %s)", resp.status_code)
            else:
                logger.warning("[custom_webhook] HTTP %s", resp.status_code)
            return ok
        except Exception as exc:
            logger.error("[custom_webhook] send failed: %s", exc)
            return False

    @staticmethod
    def _build_payload(message: str, payload_key: str) -> Dict[str, Any]:
        """Build a nested dict from *payload_key* and *message*.

        E.g. payload_key="content.msg" produces {"content": {"msg": message}}.
        """
        parts = payload_key.split(".")
        payload: Dict[str, Any] = {}
        current = payload
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                current[part] = message
            else:
                current[part] = {}
                current = current[part]
        return payload

    @staticmethod
    def _check_response(resp: requests.Response, check_expr: str) -> bool:
        """Evaluate *check_expr* against the response JSON.

        Supports simple expressions like "errcode == 0" or "code == 0".
        """
        try:
            body = resp.json()
        except Exception:
            logger.warning("[custom_webhook] response is not JSON, cannot evaluate check")
            return False

        # Simple key == value checks
        if "==" in check_expr:
            key, val = check_expr.split("==", 1)
            key = key.strip()
            val = val.strip()
            actual = body.get(key)
            # Try numeric comparison
            try:
                return int(actual) == int(val)
            except (ValueError, TypeError):
                return str(actual) == val

        # Check if a key is truthy
        key = check_expr.strip()
        return bool(body.get(key))
