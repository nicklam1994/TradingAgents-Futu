# -*- coding: utf-8 -*-
"""
Bot 平台模块

提供多平台机器人接入能力（钉钉、飞书、Discord、Telegram）。
"""

from tradingagents.notification.bot.base import (
    BotManager,
    BotMessage,
    BotPlatform,
    BotResponse,
    ChatType,
    WebhookResponse,
)
from tradingagents.notification.bot.dispatcher import (
    BotCommand,
    CommandDispatcher,
    RateLimiter,
)
from tradingagents.notification.bot.dingtalk import DingTalkPlatform
from tradingagents.notification.bot.feishu import FeishuPlatform
from tradingagents.notification.bot.discord import DiscordPlatform
from tradingagents.notification.bot.telegram import TelegramPlatform

__all__ = [
    # Base
    "BotPlatform",
    "BotManager",
    "BotMessage",
    "BotResponse",
    "WebhookResponse",
    "ChatType",
    # Dispatcher
    "BotCommand",
    "CommandDispatcher",
    "RateLimiter",
    # Platforms
    "DingTalkPlatform",
    "FeishuPlatform",
    "DiscordPlatform",
    "TelegramPlatform",
]
