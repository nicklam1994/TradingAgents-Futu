"""Bot interaction platform — multi-platform messaging bot layer.

This package provides:
  - BotPlatform ABC: abstract base for all messaging platform integrations
  - BotManager: lifecycle orchestrator for multiple bot instances
  - CommandParser: extract structured commands from natural-language messages
  - Platform implementations: DingTalk, Feishu (Lark), Discord, Telegram
  - BotCommandHandler: routes parsed commands to TradingAgents analysis

Usage:
    from api.services.bot import BotManager, DingTalkBot, TelegramBot, BotCommandHandler

    manager = BotManager()
    handler = BotCommandHandler(analyze_fn=my_analyze_coroutine)

    manager.register(DingTalkBot())
    manager.register(TelegramBot())
    manager.on_message(handler.handle)

    await manager.start_all()
"""

from .bot_platform import (
    BotManager,
    BotMessage,
    BotPlatform,
    BotPlatformType,
    BotResponse,
    CommandParser,
    ParsedCommand,
)
from .command_handler import BotCommandHandler
from .dingtalk import DingTalkBot
from .discord import DiscordBot
from .feishu import FeishuBot
from .telegram import TelegramBot

__all__ = [
    "BotManager",
    "BotMessage",
    "BotPlatform",
    "BotPlatformType",
    "BotResponse",
    "BotCommandHandler",
    "CommandParser",
    "ParsedCommand",
    "DingTalkBot",
    "DiscordBot",
    "FeishuBot",
    "TelegramBot",
]
