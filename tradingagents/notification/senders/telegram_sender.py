# -*- coding: utf-8 -*-
"""
Telegram 发送器

通过 Telegram Bot API 发送 Markdown 格式消息。
支持消息分段、重试和 topic thread。
"""

from __future__ import annotations

import logging
import time as _time
from typing import Any, Dict, Optional

import requests

from tradingagents.notification.core import NotificationChannel, Sender

logger = logging.getLogger(__name__)

# Telegram 单条消息字符上限
_TELEGRAM_MAX_LENGTH = 4096


class TelegramSender(Sender):
    """Telegram Bot API 发送器。"""

    channel = NotificationChannel.TELEGRAM

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._bot_token: str = config.get("telegram_bot_token", "") or ""
        self._chat_id: str = config.get("telegram_chat_id", "") or ""
        self._message_thread_id: Optional[str] = config.get("telegram_message_thread_id") or None

    def is_configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    def send(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        if not self.is_configured():
            logger.warning("Telegram 配置不完整，跳过推送")
            return False

        api_url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        text = self._convert_to_telegram_markdown(content)

        if title:
            text = f"*{self._escape_telegram_markdown(title)}*\n\n{text}"

        if len(text) <= _TELEGRAM_MAX_LENGTH:
            return self._send_message(api_url, text, timeout_seconds=timeout_seconds)
        else:
            return self._send_chunked(api_url, text, timeout_seconds=timeout_seconds)

    def _send_message(
        self,
        api_url: str,
        text: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """发送单条 Telegram 消息，带指数退避重试。"""
        payload: Dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if self._message_thread_id:
            payload["message_thread_id"] = self._message_thread_id

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    api_url, json=payload, timeout=timeout_seconds or 10,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt < max_retries:
                    delay = 2 ** attempt
                    logger.warning("Telegram 请求失败 (attempt %d/%d): %s, %ds 后重试",
                                   attempt, max_retries, exc, delay)
                    _time.sleep(delay)
                    continue
                logger.error("Telegram 请求失败 (已重试 %d 次): %s", max_retries, exc)
                return False

            if resp.status_code == 200:
                result = resp.json()
                if result.get("ok"):
                    logger.info("Telegram 消息发送成功")
                    return True
                logger.error("Telegram API 错误: %s", result)
                return False

            # 429 Too Many Requests
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                logger.warning("Telegram 限流，%ds 后重试", retry_after)
                _time.sleep(retry_after)
                continue

            logger.error("Telegram HTTP %d: %s", resp.status_code, resp.text[:200])
            return False

        return False

    def _send_chunked(
        self,
        api_url: str,
        text: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """分段发送长消息。"""
        chunks = self._split_text(text, _TELEGRAM_MAX_LENGTH)
        all_ok = True
        for chunk in chunks:
            if not self._send_message(api_url, chunk, timeout_seconds=timeout_seconds):
                all_ok = False
        return all_ok

    @staticmethod
    def _escape_telegram_markdown(text: str) -> str:
        """转义 Telegram Markdown V1 特殊字符。"""
        for char in ("_", "*", "`", "["):
            text = text.replace(char, f"\\{char}")
        return text

    @staticmethod
    def _convert_to_telegram_markdown(text: str) -> str:
        """将标准 Markdown 转为 Telegram 兼容格式。"""
        import re
        # 保留 **bold** 和 *italic*
        # 将 ### 标题转为粗体
        text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
        # 将 --- 分隔线转为纯文本
        text = re.sub(r"^---+$", "─────────", text, flags=re.MULTILINE)
        return text

    @staticmethod
    def _split_text(text: str, max_length: int) -> list[str]:
        """按字符长度分段，不破坏行完整性。"""
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for line in lines:
            line_len = len(line) + 1  # +1 for newline
            if current_len + line_len > max_length and current:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
            else:
                current.append(line)
                current_len += line_len

        if current:
            chunks.append("\n".join(current))
        return chunks or [text]
