# -*- coding: utf-8 -*-
"""
Bot 平台基类 + 统一消息模型

职责：
1. BotMessage / BotResponse / WebhookResponse 统一模型
2. BotPlatform ABC —— 平台适配器抽象基类
3. BotManager —— 平台注册 + Webhook 路由
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 统一消息模型
# ---------------------------------------------------------------------------

class ChatType(str, Enum):
    """会话类型"""
    GROUP = "group"
    PRIVATE = "private"
    UNKNOWN = "unknown"


@dataclass
class BotMessage:
    """统一的机器人消息模型。

    将各平台的消息格式统一为此模型，便于命令处理器处理。
    """
    platform: str
    message_id: str
    user_id: str
    user_name: str
    chat_id: str
    chat_type: ChatType
    content: str
    raw_content: str = ""
    mentioned: bool = False
    mentions: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def get_command_and_args(self, prefix: str = "/") -> Tuple[Optional[str], List[str]]:
        """解析命令和参数。

        Returns:
            (command, args) 元组，如 ("analyze", ["600519"])。
            如果不是命令，返回 (None, [])。
        """
        text = self.content.strip()

        # 中文命令映射
        if not text.startswith(prefix):
            chinese_commands = {
                "分析": "analyze",
                "大盘": "market",
                "批量": "batch",
                "帮助": "help",
                "状态": "status",
            }
            for cn_cmd, en_cmd in chinese_commands.items():
                if text.startswith(cn_cmd):
                    args = text[len(cn_cmd):].strip().split()
                    return en_cmd, args
            return None, []

        text = text[len(prefix):]
        parts = text.split()
        if not parts:
            return None, []

        return parts[0].lower(), parts[1:]

    def is_command(self, prefix: str = "/") -> bool:
        """检查消息是否是命令。"""
        cmd, _ = self.get_command_and_args(prefix)
        return cmd is not None


@dataclass
class BotResponse:
    """统一的机器人响应模型。"""
    text: str
    markdown: bool = False
    at_user: bool = True
    reply_to_message: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text_response(cls, text: str, at_user: bool = True) -> "BotResponse":
        return cls(text=text, markdown=False, at_user=at_user)

    @classmethod
    def markdown_response(cls, text: str, at_user: bool = True) -> "BotResponse":
        return cls(text=text, markdown=True, at_user=at_user)

    @classmethod
    def error_response(cls, message: str) -> "BotResponse":
        return cls(text=f"❌ 错误：{message}", markdown=False, at_user=True)


@dataclass
class WebhookResponse:
    """Webhook HTTP 响应模型。"""
    status_code: int = 200
    body: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def success(cls, body: Optional[Dict] = None) -> "WebhookResponse":
        return cls(status_code=200, body=body or {})

    @classmethod
    def challenge(cls, challenge: str) -> "WebhookResponse":
        return cls(status_code=200, body={"challenge": challenge})

    @classmethod
    def error(cls, message: str, status_code: int = 400) -> "WebhookResponse":
        return cls(status_code=status_code, body={"error": message})


# ---------------------------------------------------------------------------
# BotPlatform ABC
# ---------------------------------------------------------------------------

class BotPlatform(ABC):
    """平台适配器抽象基类。

    负责：
    1. 验证 Webhook 请求签名
    2. 解析平台消息为统一格式
    3. 将响应转换为平台格式
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """平台标识名称（如 "feishu", "dingtalk"）。"""

    @abstractmethod
    def verify_request(self, headers: Dict[str, str], body: bytes) -> bool:
        """验证请求签名。"""

    @abstractmethod
    def parse_message(self, data: Dict[str, Any]) -> Optional[BotMessage]:
        """解析平台消息为统一格式。"""

    @abstractmethod
    def format_response(self, response: BotResponse, message: BotMessage) -> WebhookResponse:
        """将统一响应转换为平台格式。"""

    def send_followup(self, response: BotResponse, message: BotMessage) -> bool:
        """发送延迟 follow-up 消息（默认空操作）。"""
        return False

    def handle_challenge(self, data: Dict[str, Any]) -> Optional[WebhookResponse]:
        """处理平台 URL 验证请求。"""
        return None

    def handle_webhook(
        self,
        headers: Dict[str, str],
        body: bytes,
        data: Dict[str, Any],
    ) -> Tuple[Optional[BotMessage], Optional[WebhookResponse]]:
        """Webhook 主入口 —— 协调验证、解析流程。

        Returns:
            (BotMessage, WebhookResponse) 元组
            - 验证请求: (None, challenge_response)
            - 普通消息: (message, None)
            - 验证失败: (None, error_response)
        """
        challenge_response = self.handle_challenge(data)
        if challenge_response:
            return None, challenge_response

        if not self.verify_request(headers, body):
            return None, WebhookResponse.error("Invalid signature", 403)

        message = self.parse_message(data)
        return message, None


# ---------------------------------------------------------------------------
# BotManager —— 平台注册 + 路由
# ---------------------------------------------------------------------------

class BotManager:
    """Bot 平台管理器 —— 注册平台、路由 Webhook。

    使用示例::

        manager = BotManager()
        manager.register(DingTalkPlatform(config))
        manager.register(TelegramPlatform(config))

        # Webhook 入口
        msg, resp = manager.handle_webhook("dingtalk", headers, body, data)
    """

    def __init__(self) -> None:
        self._platforms: Dict[str, BotPlatform] = {}

    def register(self, platform: BotPlatform) -> None:
        """注册一个平台适配器。"""
        name = platform.platform_name.lower()
        self._platforms[name] = platform
        logger.info("已注册 Bot 平台: %s", name)

    def unregister(self, name: str) -> None:
        """注销一个平台适配器。"""
        self._platforms.pop(name.lower(), None)

    def get_platform(self, name: str) -> Optional[BotPlatform]:
        """获取指定平台。"""
        return self._platforms.get(name.lower())

    @property
    def registered_platforms(self) -> List[str]:
        """已注册平台名称列表。"""
        return list(self._platforms.keys())

    def handle_webhook(
        self,
        platform_name: str,
        headers: Dict[str, str],
        body: bytes,
        data: Dict[str, Any],
    ) -> Tuple[Optional[BotMessage], Optional[WebhookResponse]]:
        """路由 Webhook 到指定平台。"""
        platform = self._platforms.get(platform_name.lower())
        if platform is None:
            logger.warning("未知 Bot 平台: %s", platform_name)
            return None, WebhookResponse.error(f"Unknown platform: {platform_name}", 404)

        return platform.handle_webhook(headers, body, data)
