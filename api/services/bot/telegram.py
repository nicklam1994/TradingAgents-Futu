"""Telegram Bot — long-polling integration.

Uses the Telegram Bot API's ``getUpdates`` method for long-polling to
receive messages, and ``sendMessage`` to reply.

Supports:
  - Long-polling mode (default, no webhook server needed)
  - Webhook mode (optional, requires public HTTPS endpoint)
  - Slash commands: /analyze, /quote, /help, /start
  - Natural-language commands: "分析 AAPL", "行情 TSLA"

Configuration (env vars):
  TELEGRAM_BOT_TOKEN  — Bot token from @BotFather (required)
  TELEGRAM_WEBHOOK_URL — Public URL for webhook mode (optional; if unset, uses long-polling)
  TELEGRAM_ALLOWED_USERS — Comma-separated user IDs to restrict access (optional)

Requires: aiohttp.
API docs: https://core.telegram.org/bots/api
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional, Set

import aiohttp

from .bot_platform import (
    BotMessage,
    BotPlatform,
    BotPlatformType,
    BotResponse,
)

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT = 30  # seconds for long-polling
_MAX_MESSAGE_LENGTH = 4096  # Telegram's per-message char limit


class TelegramBot(BotPlatform):
    """Telegram bot using long-polling (or webhook) via the Bot API."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        super().__init__(name="telegram", config=config)

        self._token: str = config.get("token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._webhook_url: str = config.get("webhook_url") or os.getenv("TELEGRAM_WEBHOOK_URL", "")

        # Access control: set of allowed user IDs (empty = allow all)
        allowed_raw = config.get("allowed_users") or os.getenv("TELEGRAM_ALLOWED_USERS", "")
        self._allowed_users: Set[str] = {
            uid.strip() for uid in allowed_raw.split(",") if uid.strip()
        }

        # Runtime state
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset: int = 0  # Last processed update_id for getUpdates
        self._callback_handlers: list = []

    @property
    def platform_type(self) -> BotPlatformType:
        return BotPlatformType.TELEGRAM

    @property
    def _api(self) -> str:
        """Base URL for Telegram Bot API calls."""
        return _API_BASE.format(token=self._token)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Validate the bot token and optionally set a webhook."""
        if not self._token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN must be set")

        if not self._session:
            self._session = aiohttp.ClientSession()

        # Verify token by calling getMe
        async with self._session.get(f"{self._api}/getMe") as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Telegram getMe failed ({resp.status}): {text}")
            data = await resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram getMe error: {data}")
            bot_info = data.get("result", {})
            logger.info(
                "[telegram] Bot verified: @%s (id=%s)",
                bot_info.get("username"), bot_info.get("id"),
            )

        # Set webhook if URL is configured
        if self._webhook_url:
            await self._set_webhook(self._webhook_url)
        else:
            # Remove any existing webhook to use long-polling
            await self._set_webhook("")

    async def _set_webhook(self, url: str) -> None:
        """Set or remove the Telegram webhook."""
        if not self._session:
            return
        payload = {"url": url}
        async with self._session.post(f"{self._api}/setWebhook", json=payload) as resp:
            body = await resp.json()
            if body.get("ok"):
                if url:
                    logger.info("[telegram] Webhook set to %s", url)
                else:
                    logger.info("[telegram] Webhook removed (using long-polling)")
            else:
                logger.warning("[telegram] setWebhook failed: %s", body)

    async def _disconnect(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _receive_messages(self) -> None:
        """Long-polling loop using getUpdates."""
        if self._webhook_url:
            # In webhook mode, messages arrive via HTTP callbacks handled
            # externally (e.g., FastAPI endpoint). Sleep here.
            logger.info("[telegram] Webhook mode — waiting for HTTP callbacks")
            while self._running:
                await asyncio.sleep(3600)
            return

        if not self._session:
            self._session = aiohttp.ClientSession()

        logger.info("[telegram] Starting long-polling (offset=%s)", self._offset)

        while self._running:
            try:
                params = {
                    "offset": self._offset,
                    "timeout": _POLL_TIMEOUT,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                }
                async with self._session.get(
                    f"{self._api}/getUpdates", params=params, timeout=aiohttp.ClientTimeout(total=_POLL_TIMEOUT + 10)
                ) as resp:
                    if resp.status != 200:
                        logger.warning("[telegram] getUpdates failed (%s)", resp.status)
                        await asyncio.sleep(5)
                        continue

                    data = await resp.json()
                    if not data.get("ok"):
                        logger.warning("[telegram] getUpdates error: %s", data)
                        await asyncio.sleep(5)
                        continue

                    updates = data.get("result", [])
                    for update in updates:
                        update_id = update.get("update_id", 0)
                        await self._process_update(update)
                        # Advance offset past this update
                        self._offset = max(self._offset, update_id + 1)

            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                # Normal for long-polling; just retry
                continue
            except aiohttp.ClientError as exc:
                logger.warning("[telegram] Polling error: %s", exc)
                await asyncio.sleep(5)

    async def _process_update(self, update: Dict[str, Any]) -> None:
        """Process a single Telegram Update object."""
        # Handle callback query (inline keyboard button press)
        callback_query = update.get("callback_query")
        if callback_query:
            await self._handle_callback_query(callback_query)
            return

        message = update.get("message")
        if not message:
            return

        # Ignore non-text messages
        text = message.get("text", "").strip()
        if not text:
            return

        sender = message.get("from", {})
        user_id = str(sender.get("id", ""))

        # Access control
        if self._allowed_users and user_id not in self._allowed_users:
            logger.debug("[telegram] Ignored message from unauthorized user %s", user_id)
            return

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))

        bot_message = BotMessage(
            platform=BotPlatformType.TELEGRAM,
            text=text,
            user_id=user_id,
            user_name=sender.get("username", "") or sender.get("first_name", ""),
            chat_id=chat_id,
            message_id=str(message.get("message_id", "")),
            extra={
                "chat_type": chat.get("type", ""),
                "language_code": sender.get("language_code", ""),
                "is_bot": sender.get("is_bot", False),
            },
        )
        await self._handle_raw({"_message": bot_message})

    # ── Webhook callback (HTTP POST from Telegram) ───────────────────────────

    async def handle_webhook_update(self, update: Dict[str, Any]) -> None:
        """Process an incoming Telegram webhook update.

        Called from a FastAPI endpoint when Telegram POSTs a message.
        """
        await self._process_update(update)

    # ── Callback query handling (inline keyboard) ─────────────────────────

    async def _handle_callback_query(self, callback_query: Dict[str, Any]) -> None:
        """Handle inline keyboard button press."""
        query_id = callback_query.get("id", "")
        data = callback_query.get("data", "")
        sender = callback_query.get("from", {})
        user_id = str(sender.get("id", ""))
        chat = callback_query.get("message", {}).get("chat", {})
        chat_id = str(chat.get("id", ""))
        message_id = str(callback_query.get("message", {}).get("message_id", ""))

        logger.info("[telegram] Callback query: data=%s, user=%s, chat=%s", data, user_id, chat_id)

        # Answer the callback query to remove loading indicator
        await self._answer_callback_query(query_id)

        # Notify registered callback handlers
        for handler in self._callback_handlers:
            try:
                await handler(data, user_id, chat_id, message_id)
            except Exception as exc:
                logger.error("[telegram] Callback handler error: %s", exc)

    async def _answer_callback_query(self, query_id: str, text: str = "") -> None:
        """Answer a callback query to dismiss the loading indicator."""
        if not self._session:
            self._session = aiohttp.ClientSession()
        payload: Dict[str, Any] = {"callback_query_id": query_id}
        if text:
            payload["text"] = text
        try:
            async with self._session.post(f"{self._api}/answerCallbackQuery", json=payload) as resp:
                if resp.status != 200:
                    logger.warning("[telegram] answerCallbackQuery failed: %s", resp.status)
        except Exception as exc:
            logger.warning("[telegram] answerCallbackQuery error: %s", exc)

    def on_callback(self, handler: Any) -> None:
        """Register a callback query handler.

        handler signature: async def(data: str, user_id: str, chat_id: str, message_id: str) -> None
        """
        self._callback_handlers.append(handler)

    # ── Send with inline keyboard ────────────────────────────────────────

    async def send_with_keyboard(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[Dict[str, str]]],
        parse_mode: str = "",
        reply_to_message_id: str = "",
    ) -> bool:
        """Send a message with inline keyboard buttons.

        Args:
            chat_id: Target chat ID
            text: Message text
            buttons: 2D list of button dicts, each with 'text' and 'callback_data'
            parse_mode: 'Markdown' or 'HTML'
            reply_to_message_id: Message ID to reply to
        """
        if not self._session:
            self._session = aiohttp.ClientSession()

        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {
                "inline_keyboard": buttons,
            },
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id

        try:
            async with self._session.post(f"{self._api}/sendMessage", json=payload) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    if body.get("ok"):
                        logger.info("[telegram] Keyboard message sent to chat %s", chat_id)
                        return True
                    logger.warning("[telegram] API error: %s", body.get("description", "unknown"))
                    return False
                body_text = await resp.text()
                logger.warning("[telegram] Send failed (%s): %s", resp.status, body_text[:300])
                return False
        except Exception as exc:
            logger.error("[telegram] Send keyboard error: %s", exc)
            return False

    # ── Send response ────────────────────────────────────────────────────────

    async def _send_response(self, response: BotResponse) -> bool:
        """Send a message via Telegram's sendMessage API."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        chat_id = response.chat_id
        if not chat_id:
            logger.warning("[telegram] No chat_id for response")
            return False

        text = response.text
        # Truncate if exceeding Telegram's limit
        if len(text) > _MAX_MESSAGE_LENGTH:
            text = text[:_MAX_MESSAGE_LENGTH - 3] + "..."

        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }

        # Set parse mode if specified
        if response.parse_mode:
            payload["parse_mode"] = response.parse_mode

        # Reply to original message if we have one
        if response.message_id:
            payload["reply_to_message_id"] = response.message_id

        try:
            async with self._session.post(f"{self._api}/sendMessage", json=payload) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    if body.get("ok"):
                        logger.info("[telegram] Message sent to chat %s", chat_id)
                        return True
                    logger.warning("[telegram] API error: %s", body.get("description", "unknown"))
                    return False
                body_text = await resp.text()
                logger.warning("[telegram] Send failed (%s): %s", resp.status, body_text[:300])
                return False
        except Exception as exc:
            logger.error("[telegram] Send error: %s", exc)
            return False

    # ── Raw message parsing ──────────────────────────────────────────────────

    def _parse_raw_message(self, raw_data: Dict[str, Any]) -> Optional[BotMessage]:
        """Extract the pre-built BotMessage from _process_update."""
        msg = raw_data.get("_message")
        if isinstance(msg, BotMessage):
            return msg
        return None
