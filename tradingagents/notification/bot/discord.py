# -*- coding: utf-8 -*-
"""
Discord Bot 平台适配器

支持 Discord Interactions (Slash Commands) + Webhook。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

from tradingagents.notification.bot.base import (
    BotMessage, BotPlatform, BotResponse, ChatType, WebhookResponse,
)

logger = logging.getLogger(__name__)


class DiscordPlatform(BotPlatform):
    """Discord Bot 平台适配器。"""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._bot_token: str = config.get("discord_bot_token", "") or ""
        self._public_key: str = config.get("discord_interactions_public_key", "") or ""

    @property
    def platform_name(self) -> str:
        return "discord"

    def verify_request(self, headers: Dict[str, str], body: bytes) -> bool:
        """验证 Discord Ed25519 签名。"""
        if not self._public_key:
            return True

        signature = headers.get("x-signature-ed25519", "")
        timestamp = headers.get("x-signature-timestamp", "")
        if not signature or not timestamp:
            return False

        try:
            from nacl.signing import VerifyKey
            from nacl.exceptions import BadSignatureError
            verify_key = VerifyKey(bytes.fromhex(self._public_key))
            verify_key.verify(f"{timestamp}{body}".encode(), bytes.fromhex(signature))
            return True
        except ImportError:
            logger.warning("nacl 库未安装，Discord 签名验证跳过")
            return True
        except Exception as exc:
            logger.warning("Discord 签名验证失败: %s", exc)
            return False

    def parse_message(self, data: Dict[str, Any]) -> Optional[BotMessage]:
        """解析 Discord Interaction。"""
        interaction_type = data.get("type")

        # Type 2 = Application Command (Slash Command)
        if interaction_type != 2:
            # Type 1 = Ping (用于验证)
            if interaction_type == 1:
                return None
            return None

        member = data.get("member", {})
        user = member.get("user", {}) or data.get("user", {})
        channel_id = data.get("channel_id", "")

        # 解析 slash command
        command_data = data.get("data", {})
        command_name = command_data.get("name", "")
        options = command_data.get("options", [])

        # 构建命令文本: /analyze 600519
        args = [opt.get("value", "") for opt in options]
        content = f"/{command_name}" + (f" {' '.join(str(a) for a in args)}" if args else "")

        return BotMessage(
            platform=self.platform_name,
            message_id=data.get("id", ""),
            user_id=user.get("id", ""),
            user_name=user.get("username", ""),
            chat_id=channel_id,
            chat_type=ChatType.GROUP,  # Discord channels are typically group
            content=content,
            raw_content=content,
            mentioned=True,
            raw_data=data,
        )

    def format_response(self, response: BotResponse, message: BotMessage) -> WebhookResponse:
        """格式化 Discord Interaction Response。"""
        # Discord Interaction Response (type 4 = CHANNEL_MESSAGE_WITH_SOURCE)
        body: Dict[str, Any] = {
            "type": 4,
            "data": {
                "content": response.text[:2000],  # Discord 2000 char limit
                "flags": 64,  # Ephemeral (only visible to invoker)
            },
        }
        return WebhookResponse.success(body)
