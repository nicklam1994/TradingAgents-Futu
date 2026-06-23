# -*- coding: utf-8 -*-
"""
Discord 发送器

通过 Discord Webhook 发送消息。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from tradingagents.notification.core import NotificationChannel, Sender

logger = logging.getLogger(__name__)

# Discord 单条消息字符上限
_DISCORD_MAX_CHARS = 2000


class DiscordSender(Sender):
    """Discord Webhook 发送器。"""

    channel = NotificationChannel.DISCORD

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._webhook_url: str = config.get("discord_webhook_url", "") or ""
        self._bot_token: str = config.get("discord_bot_token", "") or ""
        self._channel_id: str = config.get("discord_main_channel_id", "") or ""
        self._verify_ssl: bool = config.get("webhook_verify_ssl", True)

    def is_configured(self) -> bool:
        webhook_ok = bool(self._webhook_url)
        bot_ok = bool(self._bot_token and self._channel_id)
        return webhook_ok or bot_ok

    def send(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        image_bytes: Optional[bytes] = None,
    ) -> bool:
        if not self.is_configured():
            logger.warning("Discord 配置不完整，跳过推送")
            return False

        # 优先使用 Webhook
        if self._webhook_url:
            return self._send_via_webhook(content, title=title, timeout_seconds=timeout_seconds)
        # 回退到 Bot API
        return self._send_via_bot_api(content, title=title, timeout_seconds=timeout_seconds)

    def _send_via_webhook(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        image_bytes: Optional[bytes] = None,
    ) -> bool:
        """通过 Webhook 发送。"""
        chunks = self._split_content(content, _DISCORD_MAX_CHARS)
        all_ok = True
        for chunk in chunks:
            payload: Dict[str, Any] = {"content": chunk}
            if title:
                payload["username"] = title

            try:
                resp = requests.post(
                    self._webhook_url,
                    json=payload,
                    timeout=timeout_seconds or 10,
                    verify=self._verify_ssl,
                )
                if resp.status_code in (200, 204):
                    logger.info("Discord Webhook 消息发送成功")
                else:
                    logger.error("Discord Webhook 发送失败: HTTP %d", resp.status_code)
                    all_ok = False
            except Exception as exc:
                logger.error("Discord Webhook 发送异常: %s", type(exc).__name__)
                all_ok = False

        return all_ok

    def _send_via_bot_api(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        image_bytes: Optional[bytes] = None,
    ) -> bool:
        """通过 Bot API 发送。"""
        api_url = f"https://discord.com/api/v10/channels/{self._channel_id}/messages"
        headers = {"Authorization": f"Bot {self._bot_token}"}

        chunks = self._split_content(content, _DISCORD_MAX_CHARS)
        all_ok = True
        for chunk in chunks:
            payload: Dict[str, Any] = {"content": chunk}
            try:
                resp = requests.post(
                    api_url,
                    json=payload,
                    headers=headers,
                    timeout=timeout_seconds or 10,
                )
                if resp.status_code == 200:
                    logger.info("Discord Bot API 消息发送成功")
                else:
                    logger.error("Discord Bot API 发送失败: HTTP %d", resp.status_code)
                    all_ok = False
            except Exception as exc:
                logger.error("Discord Bot API 发送异常: %s", type(exc).__name__)
                all_ok = False

        return all_ok

    @staticmethod
    def _split_content(content: str, max_chars: int) -> list[str]:
        """按字符长度分段。"""
        if len(content) <= max_chars:
            return [content]

        lines = content.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for line in lines:
            line_len = len(line) + 1
            if current_len + line_len > max_chars and current:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
            else:
                current.append(line)
                current_len += line_len

        if current:
            chunks.append("\n".join(current))
        return chunks or [content[:max_chars]]
