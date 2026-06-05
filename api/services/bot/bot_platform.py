"""Bot Platform Abstraction Layer.

Defines the abstract base class (BotPlatform) that all messaging platform
integrations must implement, along with BotManager for lifecycle orchestration
and CommandParser for extracting user intents from messages.

Supported platforms: DingTalk, Feishu (Lark), Discord, Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────────────

class BotPlatformType(str, Enum):
    DINGTALK = "dingtalk"
    FEISHU = "feishu"
    DISCORD = "discord"
    TELEGRAM = "telegram"


@dataclass
class BotMessage:
    """Normalized incoming message from any bot platform.

    All platform-specific adapters must convert their native message format
    into this structure before dispatching to the command handler.
    """
    platform: BotPlatformType
    text: str                           # Raw message text
    user_id: str                        # Platform-specific user identifier
    user_name: str = ""                 # Display name (best-effort)
    chat_id: str = ""                   # Group/channel/chat ID
    message_id: str = ""                # Platform message ID (for reply threading)
    timestamp: Optional[datetime] = None
    # Platform-specific extras (e.g., DingTalk staffId, Telegram chat_type)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BotResponse:
    """Outgoing message to be sent back via a bot platform."""
    text: str
    chat_id: str = ""                   # Target chat/channel
    message_id: str = ""                # Reply-to message ID (if supported)
    parse_mode: str = ""                # "markdown", "html", etc.
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedCommand:
    """Result of parsing a user message into a structured command."""
    command: str                        # e.g., "analyze", "help", "status"
    symbol: str = ""                    # Stock ticker symbol (e.g., "AAPL", "00700")
    raw_text: str = ""                  # Original message text
    args: Dict[str, Any] = field(default_factory=dict)


# ── Command parsing ──────────────────────────────────────────────────────────

# Patterns for common commands across all platforms.
# Each pattern maps to (command_name, symbol_group_index).
_COMMAND_PATTERNS: List[tuple[re.Pattern, str, int]] = [
    # "分析 AAPL" / "分析AAPL" / "分析 00700.HK"
    (re.compile(r"^(?:分析|analyze|ana)\s+([A-Za-z0-9.]+)$", re.IGNORECASE), "analyze", 1),
    # "/analyze AAPL" (slash command)
    (re.compile(r"^/analyze\s+([A-Za-z0-9.]+)$", re.IGNORECASE), "analyze", 1),
    # "/analyze AAPL short" with horizon
    (re.compile(r"^/analyze\s+([A-Za-z0-9.]+)\s+(short|medium|long)$", re.IGNORECASE), "analyze_with_horizon", 1),
    # "行情 AAPL" / "quote AAPL"
    (re.compile(r"^(?:行情|quote|q)\s+([A-Za-z0-9.]+)$", re.IGNORECASE), "quote", 1),
    # "/quote AAPL"
    (re.compile(r"^/quote\s+([A-Za-z0-9.]+)$", re.IGNORECASE), "quote", 1),
    # "/help" or "帮助"
    (re.compile(r"^/(?:help|帮助)$", re.IGNORECASE), "help", 0),
    (re.compile(r"^帮助$", re.IGNORECASE), "help", 0),
    # "/status" or "状态"
    (re.compile(r"^/(?:status|状态)$", re.IGNORECASE), "status", 0),
    (re.compile(r"^状态$", re.IGNORECASE), "status", 0),
    # /start (Telegram convention)
    (re.compile(r"^/start$", re.IGNORECASE), "help", 0),
]


class CommandParser:
    """Extract structured commands from natural-language bot messages.

    Supports both Chinese and English command formats, as well as
    slash-commands (Telegram/Discord convention).
    """

    @staticmethod
    def parse(text: str) -> Optional[ParsedCommand]:
        """Try to parse text into a ParsedCommand. Returns None if no match."""
        text = text.strip()
        if not text:
            return None

        for pattern, command, symbol_idx in _COMMAND_PATTERNS:
            m = pattern.match(text)
            if m:
                symbol = m.group(symbol_idx).upper().strip() if symbol_idx > 0 else ""
                cmd = ParsedCommand(
                    command=command,
                    symbol=symbol,
                    raw_text=text,
                )
                # Extract horizon for analyze_with_horizon
                if command == "analyze_with_horizon":
                    cmd.command = "analyze"
                    cmd.args["horizon"] = m.group(2).lower()
                return cmd

        # Fallback: bare ticker-like string (e.g. just "AAPL" or "00700")
        if re.match(r"^[A-Za-z0-9.]{1,12}$", text):
            return ParsedCommand(command="analyze", symbol=text.upper(), raw_text=text)

        return None

    @staticmethod
    def help_text() -> str:
        """Return the help message for bot users."""
        return (
            "TradingAgents Bot 指令：\n"
            "  分析 <代码>    — 触发多 Agent 协作分析 (如: 分析 AAPL)\n"
            "  行情 <代码>    — 查询实时行情 (如: 行情 00700)\n"
            "  状态            — 查看系统运行状态\n"
            "  帮助            — 显示本帮助\n"
            "\n"
            "支持的代码格式：\n"
            "  美股: AAPL, TSLA, NVDA\n"
            "  港股: 00700, 09988\n"
            "  A股: 600519, 000001"
        )


# ── Bot Platform ABC ─────────────────────────────────────────────────────────

# Type alias for the async message handler callback.
# It receives a BotMessage and returns a BotResponse (or None to skip reply).
MessageHandler = Callable[[BotMessage], Coroutine[Any, Any, Optional[BotResponse]]]


class BotPlatform(ABC):
    """Abstract base class for all bot platform integrations.

    Subclasses must implement:
      - _connect(): establish connection (WebSocket, long-poll, etc.)
      - _disconnect(): tear down connection
      - _send_response(response): send a BotResponse back to the platform
      - platform_type (property): return the BotPlatformType enum value

    The base class handles:
      - Message normalization (via _parse_raw_message)
      - Command dispatch through the registered handler
      - Lifecycle management (start/stop with reconnect)
    """

    def __init__(self, name: str, config: Dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self._handler: Optional[MessageHandler] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._reconnect_delay: float = config.get("reconnect_delay", 5.0)
        self._max_reconnect_delay: float = config.get("max_reconnect_delay", 60.0)

    @property
    @abstractmethod
    def platform_type(self) -> BotPlatformType:
        """Return the platform type identifier."""

    @abstractmethod
    async def _connect(self) -> None:
        """Establish the platform connection. Called once per lifecycle."""

    @abstractmethod
    async def _disconnect(self) -> None:
        """Tear down the platform connection."""

    @abstractmethod
    async def _send_response(self, response: BotResponse) -> bool:
        """Send a response message. Return True on success."""

    @abstractmethod
    async def _receive_messages(self) -> None:
        """Blocking receive loop — call self._handle_raw() for each message.

        This runs inside the _run_loop coroutine.  Implementations should
        await on their native receive mechanism (WebSocket recv, HTTP poll, etc.)
        and call ``self._handle_raw(platform_type, raw_data)`` for each message.
        """

    def on_message(self, handler: MessageHandler) -> None:
        """Register the async message handler callback."""
        self._handler = handler

    async def start(self) -> None:
        """Start the bot in a background task with automatic reconnection."""
        if self._running:
            logger.warning(f"[{self.name}] Already running, skipping start()")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name=f"bot-{self.name}")
        logger.info(f"[{self.name}] Started ({self.platform_type.value})")

    async def stop(self) -> None:
        """Stop the bot and cancel the background task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await self._disconnect()
        except Exception as exc:
            logger.warning(f"[{self.name}] Error during disconnect: {exc}")
        logger.info(f"[{self.name}] Stopped")

    async def send(self, text: str, chat_id: str = "", **kwargs: Any) -> bool:
        """Convenience: send a text response via the platform."""
        response = BotResponse(text=text, chat_id=chat_id, **kwargs)
        return await self._send_response(response)

    async def _run_loop(self) -> None:
        """Main loop with exponential-backoff reconnection."""
        delay = self._reconnect_delay
        while self._running:
            try:
                await self._connect()
                delay = self._reconnect_delay  # reset on successful connect
                await self._receive_messages()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.error(f"[{self.name}] Connection error: {exc}", exc_info=True)
                logger.info(f"[{self.name}] Reconnecting in {delay:.0f}s...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def _handle_raw(self, raw_data: Dict[str, Any]) -> None:
        """Parse a raw platform message and dispatch to the registered handler.

        Subclasses call this from _receive_messages() with platform-specific
        raw data.  The base implementation calls _parse_raw_message (which
        subclasses should override) then dispatches to the handler.
        """
        try:
            message = self._parse_raw_message(raw_data)
        except Exception as exc:
            logger.warning(f"[{self.name}] Failed to parse message: {exc}")
            return

        if message is None:
            return  # Not a user message (e.g., system event, ack)

        if self._handler is None:
            logger.debug(f"[{self.name}] No handler registered, dropping message")
            return

        try:
            response = await self._handler(message)
            if response:
                if not response.chat_id:
                    response.chat_id = message.chat_id
                await self._send_response(response)
        except Exception as exc:
            logger.error(f"[{self.name}] Handler error: {exc}", exc_info=True)
            # Try to send an error message back to the user
            try:
                await self.send("⚠️ 处理消息时出错，请稍后重试。", chat_id=message.chat_id)
            except Exception:
                pass

    @abstractmethod
    def _parse_raw_message(self, raw_data: Dict[str, Any]) -> Optional[BotMessage]:
        """Convert platform-specific raw data into a BotMessage.

        Return None for non-user messages (system events, acks, etc.).
        """


# ── Bot Manager ──────────────────────────────────────────────────────────────

class BotManager:
    """Manages the lifecycle of multiple BotPlatform instances.

    Usage:
        manager = BotManager()
        manager.register(dingtalk_bot)
        manager.register(telegram_bot)
        manager.on_message(my_handler)
        await manager.start_all()
        # ... later ...
        await manager.stop_all()
    """

    def __init__(self) -> None:
        self._bots: Dict[str, BotPlatform] = {}
        self._handler: Optional[MessageHandler] = None

    def register(self, bot: BotPlatform) -> None:
        """Register a bot platform instance."""
        if bot.name in self._bots:
            raise ValueError(f"Bot '{bot.name}' already registered")
        self._bots[bot.name] = bot
        if self._handler:
            bot.on_message(self._handler)
        logger.info(f"[BotManager] Registered bot: {bot.name} ({bot.platform_type.value})")

    def unregister(self, name: str) -> None:
        """Remove a bot by name."""
        self._bots.pop(name, None)

    def on_message(self, handler: MessageHandler) -> None:
        """Set the message handler for all registered bots."""
        self._handler = handler
        for bot in self._bots.values():
            bot.on_message(handler)

    def get_bot(self, name: str) -> Optional[BotPlatform]:
        """Get a registered bot by name."""
        return self._bots.get(name)

    def get_bot_by_platform(self, platform_type: BotPlatformType) -> Optional[BotPlatform]:
        """Get the first bot matching the given platform type."""
        for bot in self._bots.values():
            if bot.platform_type == platform_type:
                return bot
        return None

    @property
    def bots(self) -> Dict[str, BotPlatform]:
        """Return all registered bots."""
        return dict(self._bots)

    async def start_all(self) -> None:
        """Start all registered bots."""
        if not self._bots:
            logger.warning("[BotManager] No bots registered")
            return
        tasks = [bot.start() for bot in self._bots.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"[BotManager] Started {len(self._bots)} bots")

    async def stop_all(self) -> None:
        """Stop all registered bots."""
        tasks = [bot.stop() for bot in self._bots.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("[BotManager] All bots stopped")

    def list_active(self) -> List[str]:
        """Return names of currently running bots."""
        return [name for name, bot in self._bots.items() if bot._running]
