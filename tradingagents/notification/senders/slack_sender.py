# -*- coding: utf-8 -*-
"""
Slack 发送器

通过 Slack Incoming Webhook 或 Bot API 发送消息。
Bot API 优先，保证文本与图片使用同一传输通道。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import requests

from tradingagents.notification.core import NotificationChannel, Sender

logger = logging.getLogger(__name__)

# Slack Block Kit 单个 section text 上限
_BLOCK_TEXT_LIMIT = 3000
# Slack text 字段上限（保守值）
_TEXT_LIMIT = 39000


class SlackSender(Sender):
    """Slack Webhook / Bot API 发送器。"""

    channel = NotificationChannel.SLACK

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._webhook_url: str = config.get("slack_webhook_url", "") or ""
        self._bot_token: str = config.get("slack_bot_token", "") or ""
        self._channel_id: str = config.get("slack_channel_id", "") or ""
        self._verify_ssl: bool = config.get("webhook_verify_ssl", True)

    @property
    def _use_bot(self) -> bool:
        """Bot 配置完整时优先走 Bot API。"""
        return bool(self._bot_token and self._channel_id)

    def is_configured(self) -> bool:
        return self._use_bot or bool(self._webhook_url)

    def send(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        image_bytes: Optional[bytes] = None,
    ) -> bool:
        if not self.is_configured():
            logger.warning("Slack 配置不完整，跳过推送")
            return False

        if self._use_bot:
            return self._send_via_bot(content, title=title, timeout_seconds=timeout_seconds)
        return self._send_via_webhook(content, title=title, timeout_seconds=timeout_seconds)

    def _send_via_webhook(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        image_bytes: Optional[bytes] = None,
    ) -> bool:
        """通过 Incoming Webhook 发送。"""
        # 构建 Block Kit payload
        blocks: list[Dict[str, Any]] = []
        if title:
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": title, "emoji": True},
            })

        # 分段添加文本
        chunks = self._split_content(content, _BLOCK_TEXT_LIMIT)
        for chunk in chunks:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk},
            })

        payload: Dict[str, Any] = {"blocks": blocks}

        try:
            resp = requests.post(
                self._webhook_url,
                json=payload,
                timeout=timeout_seconds or 10,
                verify=self._verify_ssl,
            )
            if resp.status_code == 200 and resp.text == "ok":
                logger.info("Slack Webhook 消息发送成功")
                return True
            logger.error("Slack Webhook 发送失败: HTTP %d, %s", resp.status_code, resp.text[:200])
            return False
        except Exception as exc:
            logger.error("Slack Webhook 发送异常: %s", exc)
            return False

    def _send_via_bot(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        image_bytes: Optional[bytes] = None,
    ) -> bool:
        """通过 Bot API (chat.postMessage) 发送。"""
        api_url = "https://slack.com/api/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {self._bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        # 构建 Block Kit
        blocks: list[Dict[str, Any]] = []
        if title:
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": title, "emoji": True},
            })

        chunks = self._split_content(content, _BLOCK_TEXT_LIMIT)
        for chunk in chunks:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk},
            })

        payload: Dict[str, Any] = {
            "channel": self._channel_id,
            "blocks": blocks,
            "text": content[:_TEXT_LIMIT],  # fallback text
        }

        try:
            resp = requests.post(
                api_url,
                data=json.dumps(payload),
                headers=headers,
                timeout=timeout_seconds or 10,
            )
            data = resp.json()
            if data.get("ok"):
                logger.info("Slack Bot API 消息发送成功")
                return True
            logger.error("Slack Bot API 发送失败: %s", data.get("error", "unknown"))
            return False
        except Exception as exc:
            logger.error("Slack Bot API 发送异常: %s", exc)
            return False

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
