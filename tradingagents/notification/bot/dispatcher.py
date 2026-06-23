# -*- coding: utf-8 -*-
"""
命令分发器

职责：
1. 注册和管理命令处理器
2. 解析消息中的命令和参数
3. 分发命令到对应处理器
4. 频率限制
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Protocol

from tradingagents.notification.bot.base import BotMessage, BotResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 命令处理器协议
# ---------------------------------------------------------------------------

class BotCommand(Protocol):
    """命令处理器协议。"""

    @property
    def name(self) -> str:
        """命令名称（如 "analyze"）。"""
        ...

    @property
    def aliases(self) -> List[str]:
        """命令别名列表。"""
        ...

    @property
    def description(self) -> str:
        """命令描述。"""
        ...

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行命令。"""
        ...


# ---------------------------------------------------------------------------
# 频率限制器
# ---------------------------------------------------------------------------

class RateLimiter:
    """基于滑动窗口的频率限制器。"""

    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, user_id: str) -> bool:
        """检查用户是否允许请求。"""
        now = time.time()
        window_start = now - self.window_seconds
        self._requests[user_id] = [t for t in self._requests[user_id] if t > window_start]

        if len(self._requests[user_id]) >= self.max_requests:
            return False

        self._requests[user_id].append(now)
        return True

    def get_remaining(self, user_id: str) -> int:
        """获取剩余可用请求数。"""
        now = time.time()
        window_start = now - self.window_seconds
        self._requests[user_id] = [t for t in self._requests[user_id] if t > window_start]
        return max(0, self.max_requests - len(self._requests[user_id]))


# ---------------------------------------------------------------------------
# 命令分发器
# ---------------------------------------------------------------------------

class CommandDispatcher:
    """命令分发器 —— 注册命令、路由分发。

    使用示例::

        dispatcher = CommandDispatcher()
        dispatcher.register(AnalyzeCommand())

        response = dispatcher.dispatch(message)
    """

    def __init__(
        self,
        command_prefix: str = "/",
        rate_limit_requests: int = 10,
        rate_limit_window: int = 60,
        admin_users: Optional[List[str]] = None,
    ) -> None:
        self.command_prefix = command_prefix
        self.admin_users = set(admin_users or [])
        self._commands: Dict[str, BotCommand] = {}
        self._aliases: Dict[str, str] = {}
        self._rate_limiter = RateLimiter(rate_limit_requests, rate_limit_window)

    def register(self, command: BotCommand) -> None:
        """注册命令。"""
        name = command.name.lower()
        if name in self._commands:
            logger.warning("命令 '%s' 已存在，将被覆盖", name)
        self._commands[name] = command
        logger.debug("注册命令: %s", name)

        for alias in command.aliases:
            alias_lower = alias.lower()
            self._aliases[alias_lower] = name

    def unregister(self, name: str) -> bool:
        """注销命令。"""
        name = name.lower()
        if name not in self._commands:
            return False
        command = self._commands.pop(name)
        for alias in command.aliases:
            self._aliases.pop(alias.lower(), None)
        return True

    def get_command(self, name: str) -> Optional[BotCommand]:
        """获取命令（支持别名）。"""
        name = name.lower()
        if name in self._commands:
            return self._commands[name]
        alias_target = self._aliases.get(name)
        if alias_target:
            return self._commands.get(alias_target)
        return None

    def list_commands(self) -> List[Dict[str, str]]:
        """列出所有已注册命令。"""
        seen: set[str] = set()
        result: List[Dict[str, str]] = []
        for name, cmd in self._commands.items():
            if name not in seen:
                seen.add(name)
                result.append({
                    "name": name,
                    "description": cmd.description,
                    "aliases": ", ".join(cmd.aliases) if cmd.aliases else "",
                })
        return result

    def dispatch(self, message: BotMessage) -> BotResponse:
        """分发消息到对应命令处理器。

        Args:
            message: 统一消息模型。

        Returns:
            BotResponse
        """
        # 1. 频率限制
        if not self._rate_limiter.is_allowed(message.user_id):
            remaining = self._rate_limiter.get_remaining(message.user_id)
            return BotResponse.text_response(
                f"⚠️ 请求过于频繁，请稍后再试（剩余 {remaining} 次）"
            )

        # 2. 解析命令
        command_name, args = message.get_command_and_args(self.command_prefix)

        if command_name is None:
            return BotResponse.text_response(
                f"❓ 未识别的命令，发送 {self.command_prefix}help 查看帮助"
            )

        # 3. 查找命令处理器
        command = self.get_command(command_name)
        if command is None:
            return BotResponse.text_response(
                f"❓ 未知命令: {command_name}，发送 {self.command_prefix}help 查看帮助"
            )

        # 4. 执行命令
        try:
            return command.execute(message, args)
        except Exception as exc:
            logger.error("命令 '%s' 执行失败: %s", command_name, exc, exc_info=True)
            return BotResponse.error_response(f"命令执行失败: {exc}")
