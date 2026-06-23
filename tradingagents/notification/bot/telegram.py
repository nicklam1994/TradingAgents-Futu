# -*- coding: utf-8 -*-
"""
Telegram Bot 平台适配器

接收 Telegram Bot Webhook 更新，解析为统一消息格式。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

from tradingagents.notification.bot.base import (
    BotMessage, BotPlatform, BotResponse, ChatType, WebhookResponse,
)

logger = logging.getLogger(__name__)


class TelegramPlatform(BotPlatform):
    """Telegram Bot 平台适配器。"""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._bot_token: str = config.get("telegram_bot_token", "") or ""
        self._webhook_secret: str = config.get("telegram_webhook_secret", "") or ""

    @property
    def platform_name(self) -> str:
        return "telegram"

    def verify_request(self, headers: Dict[str, str], body: bytes) -> bool:
        """验证 Telegram Webhook Secret Token。"""
        if not self._webhook_secret:
            return True
        token = headers.get("x-telegram-bot-api-secret-token", "")
        return hmac.compare_digest(token, self._webhook_secret)

    def parse_message(self, data: Dict[str, Any]) -> Optional[BotMessage]:
        """解析 Telegram Update。"""
        message = data.get("message")
        if not message:
            # 可能是 edited_message 或其他类型
            message = data.get("edited_message")
        if not message:
            return None

        text = (message.get("text") or "").strip()
        if not text:
            return None

        chat = message.get("chat", {})
        sender = message.get("from", {})

        chat_type_map = {
            "private": ChatType.PRIVATE,
            "group": ChatType.GROUP,
            "supergroup": ChatType.GROUP,
            "channel": ChatType.GROUP,
        }
        chat_type = chat_type_map.get(chat.get("type", ""), ChatType.UNKNOWN)

        # 提取 @mention
        mentioned = False
        mentions: list[str] = []
        entities = message.get("entities", [])
        for entity in entities:
            if entity.get("type") == "mention":
                mentioned = True
                offset = entity.get("offset", 0)
                length = entity.get("length", 0)
                mention = text[offset:offset + length]
                mentions.append(mention)

        return BotMessage(
            platform=self.platform_name,
            message_id=str(message.get("message_id", "")),
            user_id=str(sender.get("id", "")),
            user_name=sender.get("username", "") or sender.get("first_name", ""),
            chat_id=str(chat.get("id", "")),
            chat_type=chat_type,
            content=text,
            raw_content=text,
            mentioned=mentioned,
            mentions=mentions,
            raw_data=data,
        )

    def format_response(self, response: BotResponse, message: BotMessage) -> WebhookResponse:
        """格式化 Telegram 响应（通过 Bot API 发送，非 inline response）。"""
        # Telegram Webhook 通常返回 200 空响应，
        # 实际回复通过 Bot API sendMessage 发送。
        # 这里返回一个标记，让上层调用 Bot API。
        return WebhookResponse.success({
            "_telegram_reply": True,
            "chat_id": message.chat_id,
            "text": response.text,
            "parse_mode": "Markdown" if response.markdown else None,
            "reply_to_message_id": message.message_id if response.reply_to_message else None,
        })
