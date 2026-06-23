# -*- coding: utf-8 -*-
"""
企业微信发送器

通过企业微信 Webhook 发送文本/Markdown 消息。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from tradingagents.notification.core import NotificationChannel, Sender

logger = logging.getLogger(__name__)

# WeChat Work 单条消息字节上限（保守值）
_WECHAT_MAX_BYTES = 4000


class WechatSender(Sender):
    """企业微信 Webhook 发送器。"""

    channel = NotificationChannel.WECHAT

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._webhook_url: str = config.get("wechat_webhook_url", "") or ""
        self._msg_type: str = config.get("wechat_msg_type", "markdown") or "markdown"
        self._verify_ssl: bool = config.get("webhook_verify_ssl", True)

    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    def send(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        image_bytes: Optional[bytes] = None,
    ) -> bool:
        if not self.is_configured():
            logger.warning("企业微信 Webhook 未配置，跳过推送")
            return False

        chunks = self._split_content(content, _WECHAT_MAX_BYTES)
        all_ok = True
        for chunk in chunks:
            if self._msg_type == "markdown":
                payload: Dict[str, Any] = {"msgtype": "markdown", "markdown": {"content": chunk}}
            else:
                payload = {"msgtype": "text", "text": {"content": chunk}}

            try:
                resp = requests.post(
                    self._webhook_url,
                    json=payload,
                    timeout=timeout_seconds or 10,
                    verify=self._verify_ssl,
                )
                data = resp.json()
                if data.get("errcode", 0) != 0:
                    logger.error("企业微信发送失败: errcode=%s", data.get("errcode"))
                    all_ok = False
                else:
                    logger.info("企业微信消息发送成功")
            except Exception as exc:
                logger.error("企业微信发送异常: %s", type(exc).__name__)
                all_ok = False

        return all_ok

    @staticmethod
    def _split_content(content: str, max_bytes: int) -> list[str]:
        """按字节长度分段，不破坏行完整性。"""
        lines = content.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_bytes = 0

        for line in lines:
            line_bytes = len(line.encode("utf-8")) + 1  # +1 for newline
            if current_bytes + line_bytes > max_bytes and current:
                chunks.append("\n".join(current))
                current = [line]
                current_bytes = line_bytes
            else:
                current.append(line)
                current_bytes += line_bytes

        if current:
            chunks.append("\n".join(current))
        return chunks or [content]
