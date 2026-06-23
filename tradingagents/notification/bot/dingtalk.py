# -*- coding: utf-8 -*-
"""
钉钉 Bot 平台适配器

支持钉钉 Stream + Webhook 两种接入方式。
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import logging
import time
from typing import Any, Dict, Optional

from tradingagents.notification.bot.base import (
    BotMessage, BotPlatform, BotResponse, ChatType, WebhookResponse,
)

logger = logging.getLogger(__name__)


class DingTalkPlatform(BotPlatform):
    """钉钉自定义机器人平台适配器。"""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._app_key: str = config.get("dingtalk_app_key", "") or ""
        self._app_secret: str = config.get("dingtalk_app_secret", "") or ""
        self._webhook_token: str = config.get("dingtalk_webhook_token", "") or ""
        self._webhook_secret: str = config.get("dingtalk_webhook_secret", "") or ""

    @property
    def platform_name(self) -> str:
        return "dingtalk"

    def verify_request(self, headers: Dict[str, str], body: bytes) -> bool:
        """验证钉钉 Webhook 签名。"""
        if not self._webhook_secret:
            return True  # 未配置签名则跳过验证

        timestamp = headers.get("timestamp", "")
        sign = headers.get("sign", "")
        if not timestamp or not sign:
            return False

        string_to_sign = f"{timestamp}\n{self._webhook_secret}"
        hmac_code = hmac.new(
            self._webhook_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        expected_sign = base64.b64encode(hmac_code).decode("utf-8")
        return sign == expected_sign

    def parse_message(self, data: Dict[str, Any]) -> Optional[BotMessage]:
        """解析钉钉消息。"""
        msg_data = data.get("text", {})
        content = msg_data.get("content", "").strip()
        if not content:
            # 尝试其他消息类型
            content = data.get("content", "").strip()
        if not content:
            return None

        # 提取 @机器人 的信息
        sender = data.get("senderNick", "") or data.get("senderStaffId", "")
        chat_id = data.get("conversationId", "") or data.get("chatbotCorpId", "")
        chat_type_str = data.get("conversationType", "")

        chat_type = ChatType.GROUP if chat_type_str == "2" else ChatType.PRIVATE

        return BotMessage(
            platform=self.platform_name,
            message_id=data.get("msgId", ""),
            user_id=data.get("senderStaffId", "") or data.get("senderId", ""),
            user_name=sender,
            chat_id=chat_id,
            chat_type=chat_type,
            content=content,
            raw_content=content,
            mentioned=True,  # 钉钉 Webhook 只接收 @机器人 的消息
            raw_data=data,
        )

    def format_response(self, response: BotResponse, message: BotMessage) -> WebhookResponse:
        """格式化钉钉响应。"""
        if response.markdown:
            body = {
                "msgtype": "markdown",
                "markdown": {"title": "TradingAgents", "text": response.text},
            }
        else:
            body = {
                "msgtype": "text",
                "text": {"content": response.text},
            }

        if response.at_user and message.user_id:
            body["at"] = {"atDingtalkIds": [message.user_id]}

        return WebhookResponse.success(body)
