"""Bot Command Handler — routes parsed commands to TradingAgents.

This module implements the core message handler that:
  1. Parses incoming bot messages into structured commands
  2. Routes "analyze" commands to the TradingAgents analysis pipeline
  3. Handles "quote" commands for quick price lookups
  4. Responds with help/status for informational commands

The handler is designed to be registered with BotManager via
``manager.on_message(handler.handle)``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional

from .bot_platform import (
    BotMessage,
    BotResponse,
    CommandParser,
    ParsedCommand,
)

logger = logging.getLogger(__name__)

# Type alias for the analysis trigger function.
# It receives (symbol, user_context) and returns the analysis result text.
AnalyzeFn = Callable[..., Coroutine[Any, Any, str]]


class BotCommandHandler:
    """Routes bot messages to TradingAgents commands.

    Args:
        analyze_fn: Async callable that triggers a TradingAgents analysis.
                    Signature: ``async def analyze(symbol: str, **kwargs) -> str``
                    where kwargs may include horizon, user_context, etc.
        quote_fn:   Optional async callable for quick quote lookups.
                    Signature: ``async def quote(symbol: str) -> str``
        status_fn:  Optional async callable for system status.
                    Signature: ``async def status() -> str``
        function_call_fn: Optional async callable for LLM function calling.
                    Signature: ``async def function_call(message: str, user_id: str) -> dict``
    """

    def __init__(
        self,
        analyze_fn: Optional[AnalyzeFn] = None,
        quote_fn: Optional[Callable[..., Coroutine[Any, Any, str]]] = None,
        status_fn: Optional[Callable[..., Coroutine[Any, Any, str]]] = None,
        function_call_fn: Optional[Callable[..., Coroutine[Any, Any, dict]]] = None,
    ) -> None:
        self._analyze_fn = analyze_fn
        self._quote_fn = quote_fn
        self._status_fn = status_fn
        self._function_call_fn = function_call_fn
        # Simple in-memory rate limiter: user_id -> last_command_timestamp
        self._rate_limit: Dict[str, float] = {}
        self._rate_limit_seconds: float = 10.0  # Minimum seconds between commands per user
        # Per-user chat mode: 'command' or 'function'
        self._user_mode: Dict[str, str] = {}

    async def handle(self, message: BotMessage) -> Optional[BotResponse]:
        """Main entry point: parse the message and dispatch to the right handler.

        Returns a BotResponse to send back, or None to silently ignore.
        """
        text = message.text.strip()

        # ── Mode switch commands ──────────────────────────────────────────
        if text in ('/command', '/cmd', '命令模式'):
            self._user_mode[message.user_id] = 'command'
            return BotResponse(
                text="⚡ 已切换到命令模式\n\n发送「分析 AAPL」或「行情 00700.HK」快速执行。",
                chat_id=message.chat_id,
            )
        if text in ('/chat', '对话模式'):
            self._user_mode[message.user_id] = 'function'
            return BotResponse(
                text="🧠 已切换到对话模式\n\n用自然语言对话，例如「帮我看看腾讯最近怎么样」。",
                chat_id=message.chat_id,
            )

        # ── Function calling mode (default when available) ─────────────
        user_mode = self._user_mode.get(message.user_id)
        if user_mode != 'command' and self._function_call_fn:
            # Still allow explicit slash commands
            parsed = CommandParser.parse(text)
            if parsed and parsed.command in ('help', 'status'):
                return await self._dispatch_command(message, parsed)
            # Route to LLM function calling
            return await self._handle_function_call(message)

        # ── Command mode (default) ────────────────────────────────────────
        parsed = CommandParser.parse(text)
        if parsed is None:
            return BotResponse(
                text="未识别的指令。发送「帮助」查看可用命令。\n\n💡 发送「对话模式」可切换到自然语言对话。",
                chat_id=message.chat_id,
            )
        return await self._dispatch_command(message, parsed)

    async def _dispatch_command(self, message: BotMessage, parsed) -> BotResponse:
        """Dispatch a parsed command to the appropriate handler."""
        # Rate limiting
        if not self._check_rate_limit(message.user_id):
            return BotResponse(
                text="⏳ 操作过于频繁，请稍后再试。",
                chat_id=message.chat_id,
            )

        command = parsed.command
        if command == "analyze":
            return await self._handle_analyze(message, parsed)
        elif command == "quote":
            return await self._handle_quote(message, parsed)
        elif command == "help":
            return self._handle_help(message)
        elif command == "status":
            return await self._handle_status(message)
        else:
            return BotResponse(
                text=f"未知命令: {command}。发送「帮助」查看可用命令。",
                chat_id=message.chat_id,
            )

    # ── Command handlers ─────────────────────────────────────────────────────

    async def _handle_analyze(self, message: BotMessage, parsed: ParsedCommand) -> BotResponse:
        """Trigger a TradingAgents analysis for the given symbol."""
        symbol = parsed.symbol
        if not symbol:
            return BotResponse(
                text="请提供股票代码。例如: 分析 AAPL",
                chat_id=message.chat_id,
            )

        # Normalize symbol
        symbol = symbol.upper().strip()

        # Acknowledge receipt immediately
        ack_text = f"🔍 正在分析 {symbol}，多 Agent 协作中，请稍候..."

        if self._analyze_fn is None:
            return BotResponse(
                text=f"⚠️ 分析功能未配置。收到的分析请求: {symbol}",
                chat_id=message.chat_id,
            )

        # Build kwargs for the analyze function
        kwargs: Dict[str, Any] = {}
        horizon = parsed.args.get("horizon", "short")
        kwargs["horizon"] = horizon
        kwargs["chat_id"] = message.chat_id  # For disambiguation keyboard

        try:
            # Run the analysis
            result_text = await self._analyze_fn(symbol, **kwargs)

            # Truncate if too long (Telegram limit is 4096, Discord 2000)
            max_len = 3500
            if len(result_text) > max_len:
                result_text = result_text[:max_len] + "\n\n... (结果已截断，请查看完整报告)"

            return BotResponse(
                text=f"📊 {symbol} 分析结果:\n\n{result_text}",
                chat_id=message.chat_id,
            )
        except Exception as exc:
            logger.error("[bot] Analysis failed for %s: %s", symbol, exc, exc_info=True)
            return BotResponse(
                text=f"❌ 分析 {symbol} 时出错: {exc}",
                chat_id=message.chat_id,
            )

    async def _handle_quote(self, message: BotMessage, parsed: ParsedCommand) -> BotResponse:
        """Quick quote lookup for a stock symbol."""
        symbol = parsed.symbol
        if not symbol:
            return BotResponse(
                text="请提供股票代码。例如: 行情 AAPL",
                chat_id=message.chat_id,
            )

        symbol = symbol.upper().strip()

        if self._quote_fn is None:
            return BotResponse(
                text=f"ℹ️ 行情查询功能未配置。请求的代码: {symbol}",
                chat_id=message.chat_id,
            )

        try:
            result_text = await self._quote_fn(symbol)
            return BotResponse(text=result_text, chat_id=message.chat_id)
        except Exception as exc:
            logger.error("[bot] Quote failed for %s: %s", symbol, exc, exc_info=True)
            return BotResponse(
                text=f"❌ 查询 {symbol} 行情时出错: {exc}",
                chat_id=message.chat_id,
            )

    def _handle_help(self, message: BotMessage) -> BotResponse:
        """Return the help text."""
        return BotResponse(
            text=CommandParser.help_text(),
            chat_id=message.chat_id,
        )

    async def _handle_status(self, message: BotMessage) -> BotResponse:
        """Return system status information."""
        if self._status_fn:
            try:
                status_text = await self._status_fn()
                return BotResponse(text=status_text, chat_id=message.chat_id)
            except Exception as exc:
                logger.error("[bot] Status check failed: %s", exc)
                return BotResponse(
                    text=f"⚠️ 获取状态时出错: {exc}",
                    chat_id=message.chat_id,
                )

        # Default status message when no status_fn is configured
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return BotResponse(
            text=f"✅ TradingAgents Bot 运行中\n⏰ 当前时间: {now}",
            chat_id=message.chat_id,
        )

    # ── Function calling handler ────────────────────────────────────────────

    async def _handle_function_call(self, message: BotMessage) -> BotResponse:
        """Route message through LLM function calling."""
        if not self._function_call_fn:
            return BotResponse(
                text="⚠️ 对话模式未配置。",
                chat_id=message.chat_id,
            )

        try:
            result = await self._function_call_fn(message.text, message.user_id, message.chat_id)
            if result.get("ok") and result.get("data"):
                data = result["data"]
                # Inline keyboard already sent for disambiguation — skip text reply
                if data.get("disambiguation"):
                    return None
                reply = data.get("response", "")
                tool_call = data.get("tool_call")

                if tool_call and tool_call.get("name") == "analyze_stock":
                    # Analysis was triggered — reply includes the acknowledgment
                    return BotResponse(text=reply, chat_id=message.chat_id)
                else:
                    return BotResponse(text=reply, chat_id=message.chat_id)
            else:
                error = result.get("error", "未知错误")
                return BotResponse(
                    text=f"❌ 对话失败: {error}",
                    chat_id=message.chat_id,
                )
        except Exception as exc:
            logger.error("[bot] Function call failed: %s", exc, exc_info=True)
            return BotResponse(
                text=f"❌ 对话出错: {exc}",
                chat_id=message.chat_id,
            )

    # ── Rate limiting ────────────────────────────────────────────────────────

    def _check_rate_limit(self, user_id: str) -> bool:
        """Check if the user is within the rate limit. Returns True if allowed."""
        now = time.time()
        last = self._rate_limit.get(user_id, 0)
        if now - last < self._rate_limit_seconds:
            return False
        self._rate_limit[user_id] = now
        return True
