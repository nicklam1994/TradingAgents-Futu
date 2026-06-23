# -*- coding: utf-8 -*-
"""
飞书 Bot 平台适配器

支持飞书 Stream + Webhook 两种接入方式。
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import time
from typing import Any, Dict, Optional

from tradingagents.notification.bot.base import (
    BotMessage, BotPlatform, BotResponse, ChatType, WebhookResponse,
)

logger = logging.getLogger(__name__)


class FeishuPlatform(BotPlatform):
    """飞书自定义机器人平台适配器。"""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._app_id: str = config.get("feishu_app_id", "") or ""
        self._app_secret: str = config.get("feishu_app_secret", "") or ""
        self._verification_token: str = config.get("feishu_verification_token", "") or ""
        self._encrypt_key: str = config.get("feishu_encrypt_key", "") or ""

    @property
    def platform_name(self) -> str:
        return "feishu"

    def verify_request(self, headers: Dict[str, str], body: bytes) -> bool:
        """验证飞书请求。"""
        if not self._verification_token:
            return True
        # 飞书通过 verification_token 验证
        token = headers.get("x-verification-token", "")
        return token == self._verification_token

    def handle_challenge(self, data: Dict[str, Any]) -> Optional[WebhookResponse]:
        """处理飞书 URL 验证请求。"""
        challenge = data.get("challenge")
        if challenge:
            return WebhookResponse.challenge(challenge)
        return None

    def parse_message(self, data: Dict[str, Any]) -> Optional[BotMessage]:
        """解析飞书消息。"""
        # 飞书事件结构: { "event": { "message": {...}, "sender": {...} } }
        event = data.get("event", {})
        message_data = event.get("message", {})
        sender_data = event.get("sender", {})

        content_str = message_data.get("content", "{}")
        # 飞书消息 content 是 JSON 字符串
        try:
            content_obj = json.loads(content_str)
            text = content_obj.get("text", "").strip()
        except (json.JSONDecodeError, TypeError):
            text = content_str.strip()

        if not text:
            return None

        # 去除 @机器人 的部分
        mentions = message_data.get("mentions", [])
        for mention in mentions:
            key = mention.get("key", "")
            if key:
                text = text.replace(key, "").strip()

        chat_type_str = message_data.get("chat_type", "")
        chat_type = ChatType.GROUP if chat_type_str == "group" else ChatType.PRIVATE

        return BotMessage(
            platform=self.platform_name,
            message_id=message_data.get("message_id", ""),
            user_id=sender_data.get("sender_id", {}).get("open_id", ""),
            user_name=sender_data.get("sender_id", {}).get("open_id", ""),
            chat_id=message_data.get("chat_id", ""),
            chat_type=chat_type,
            content=text,
            raw_content=content_str,
            mentioned=bool(mentions),
            mentions=[m.get("key", "") for m in mentions],
            raw_data=data,
        )

    def format_response(self, response: BotResponse, message: BotMessage) -> WebhookResponse:
        """格式化飞书响应。"""
        if response.markdown:
            body = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": response.text}},
                    ],
                },
            }
        else:
            body = {
                "msg_type": "text",
                "content": {"text": response.text},
            }

        return WebhookResponse.success(body)
