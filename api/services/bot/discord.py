"""Discord Bot — Gateway WebSocket integration.

Implements the Discord Gateway protocol v10 for receiving and sending
messages.  Uses the bot's token to authenticate via the Identify payload
and listens for MESSAGE_CREATE events.

Configuration (env vars):
  DISCORD_BOT_TOKEN   — Bot token (required)
  DISCORD_CHANNEL_ID  — Default channel ID for outbound messages (optional)

Requires: aiohttp.
Gateway docs: https://discord.com/developers/docs/topics/gateway
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

import aiohttp

from .bot_platform import (
    BotMessage,
    BotPlatform,
    BotPlatformType,
    BotResponse,
)

logger = logging.getLogger(__name__)

# Discord Gateway opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_PRESENCE_UPDATE = 3
OP_RESUME = 6
OP_RECONNECT = 7
OP_REQUEST_GUILD_MEMBERS = 8
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Gateway intents (bitfield)
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_MESSAGE_CONTENT = 1 << 15

_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
_API_BASE = "https://discord.com/api/v10"


class DiscordBot(BotPlatform):
    """Discord bot using the Gateway WebSocket protocol."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        super().__init__(name="discord", config=config)

        self._token: str = config.get("token") or os.getenv("DISCORD_BOT_TOKEN", "")
        self._default_channel_id: str = (
            config.get("channel_id") or os.getenv("DISCORD_CHANNEL_ID", "")
        )
        # Intents to subscribe to
        self._intents: int = INTENT_GUILDS | INTENT_GUILD_MESSAGES | INTENT_MESSAGE_CONTENT

        # Gateway state
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._heartbeat_interval: float = 41.25  # seconds, overridden by HELLO
        self._last_heartbeat_ack: bool = True
        self._sequence: Optional[int] = None
        self._session_id: Optional[str] = None
        self._resume_gateway_url: Optional[str] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def platform_type(self) -> BotPlatformType:
        return BotPlatformType.DISCORD

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Open a WebSocket session with Discord's Gateway."""
        if not self._token:
            raise RuntimeError("DISCORD_BOT_TOKEN must be set")

        if not self._session:
            self._session = aiohttp.ClientSession()

        # Use resume URL if available (from a previous connection)
        url = self._resume_gateway_url or _GATEWAY_URL
        self._ws = await self._session.ws_connect(url, heartbeat=None)
        logger.info("[discord] Gateway WebSocket connected")

    async def _disconnect(self) -> None:
        """Close WebSocket, stop heartbeat, and clean up."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _receive_messages(self) -> None:
        """Main receive loop for Discord Gateway events."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._handle_gateway_message(data)
                except json.JSONDecodeError:
                    logger.warning("[discord] Non-JSON gateway message: %s", msg.data[:200])
            elif msg.type == aiohttp.WSMsgType.BINARY:
                # Discord sends JSON as text; binary is unexpected
                logger.warning("[discord] Unexpected binary message")
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                logger.info("[discord] Gateway WebSocket closed")
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("[discord] WS error: %s", self._ws.exception())
                break

    # ── Gateway protocol ─────────────────────────────────────────────────────

    async def _send_gateway(self, opcode: int, payload: Any = None) -> None:
        """Send a payload to the Discord Gateway."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WebSocket not connected")
        data = {"op": opcode, "d": payload}
        await self._ws.send_json(data)

    async def _handle_gateway_message(self, data: Dict[str, Any]) -> None:
        """Process a Discord Gateway message by opcode."""
        opcode = data.get("op")
        payload = data.get("d")
        seq = data.get("s")
        event_name = data.get("t")

        if seq is not None:
            self._sequence = seq

        if opcode == OP_HELLO:
            await self._on_hello(payload)

        elif opcode == OP_HEARTBEAT:
            await self._send_gateway(OP_HEARTBEAT, self._sequence)

        elif opcode == OP_HEARTBEAT_ACK:
            self._last_heartbeat_ack = True
            logger.debug("[discord] Heartbeat ACK received")

        elif opcode == OP_INVALID_SESSION:
            logger.warning("[discord] Invalid session (resumable=%s)", payload)
            # Wait 1-5 seconds before reconnecting (per Discord docs)
            await asyncio.sleep(random.uniform(1, 5))
            if payload is True:
                await self._resume()
            else:
                await self._identify()

        elif opcode == OP_RECONNECT:
            logger.info("[discord] Gateway requested reconnect")
            # The outer _run_loop will handle reconnection
            raise ConnectionError("Gateway requested reconnect")

        elif opcode == OP_DISPATCH:
            await self._on_dispatch(event_name, payload)

    async def _on_hello(self, payload: Any) -> None:
        """Handle HELLO: start heartbeat and identify."""
        if not isinstance(payload, dict):
            payload = {}
        self._heartbeat_interval = payload.get("heartbeat_interval", 41250) / 1000.0
        logger.info("[discord] HELLO received, heartbeat_interval=%.1fs", self._heartbeat_interval)

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Identify or resume
        if self._session_id:
            await self._resume()
        else:
            await self._identify()

    async def _identify(self) -> None:
        """Send an Identify payload to authenticate."""
        identify_payload = {
            "token": f"Bot {self._token}",
            "intents": self._intents,
            "properties": {
                "os": "linux",
                "library": "tradingagents-bot",
                "device": "tradingagents-bot",
            },
        }
        await self._send_gateway(OP_IDENTIFY, identify_payload)
        logger.info("[discord] Identify sent")

    async def _resume(self) -> None:
        """Send a Resume payload to reconnect a previous session."""
        resume_payload = {
            "token": f"Bot {self._token}",
            "session_id": self._session_id or "",
            "seq": self._sequence or 0,
        }
        await self._send_gateway(OP_RESUME, resume_payload)
        logger.info("[discord] Resume sent (session=%s, seq=%s)", self._session_id, self._sequence)

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat at the interval specified by the Gateway."""
        try:
            # First heartbeat should be sent within heartbeat_interval * jitter
            # where jitter is [0, 1). We'll wait a random fraction.
            initial_wait = self._heartbeat_interval * random.random()
            await asyncio.sleep(initial_wait)

            while self._running and self._ws and not self._ws.closed:
                if not self._last_heartbeat_ack:
                    logger.warning("[discord] Heartbeat ACK missed, reconnecting")
                    # Close the WebSocket to trigger reconnection in _run_loop
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    return

                self._last_heartbeat_ack = False
                await self._send_gateway(OP_HEARTBEAT, self._sequence)
                logger.debug("[discord] Heartbeat sent (seq=%s)", self._sequence)
                await asyncio.sleep(self._heartbeat_interval)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[discord] Heartbeat loop error: %s", exc)

    async def _on_dispatch(self, event_name: Optional[str], payload: Any) -> None:
        """Handle a DISPATCH event from Discord."""
        if event_name == "READY":
            self._session_id = payload.get("session_id")
            self._resume_gateway_url = payload.get("resume_gateway_url")
            if self._resume_gateway_url:
                self._resume_gateway_url = self._resume_gateway_url.rstrip("/")
            user = payload.get("user", {})
            logger.info(
                "[discord] READY — logged in as %s#%s (id=%s)",
                user.get("username"), user.get("discriminator"), user.get("id"),
            )
            return

        if event_name == "RESUMED":
            logger.info("[discord] Session resumed")
            return

        if event_name == "MESSAGE_CREATE":
            await self._on_message_create(payload)

    async def _on_message_create(self, payload: Dict[str, Any]) -> None:
        """Handle a MESSAGE_CREATE event (new message in a channel)."""
        # Ignore messages from bots (including ourselves)
        author = payload.get("author", {})
        if author.get("bot", False):
            return

        text = (payload.get("content") or "").strip()
        if not text:
            return

        # Discord mentions use <@USER_ID> format; strip bot mention prefix
        # e.g., "<@123456789> 分析 AAPL" -> "分析 AAPL"
        import re
        text = re.sub(r"^<@!?\d+>\s*", "", text).strip()

        if not text:
            return

        message = BotMessage(
            platform=BotPlatformType.DISCORD,
            text=text,
            user_id=author.get("id", ""),
            user_name=author.get("username", ""),
            chat_id=payload.get("channel_id", ""),
            message_id=payload.get("id", ""),
            extra={
                "guild_id": payload.get("guild_id", ""),
                "author": author,
            },
        )
        await self._handle_raw({"_message": message})

    # ── Send response ────────────────────────────────────────────────────────

    async def _send_response(self, response: BotResponse) -> bool:
        """Send a message to a Discord channel via REST API."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        channel_id = response.chat_id or self._default_channel_id
        if not channel_id:
            logger.warning("[discord] No channel_id for response")
            return False

        url = f"{_API_BASE}/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {"content": response.text}

        # If replying to a specific message
        if response.message_id:
            payload["message_reference"] = {"message_id": response.message_id}

        # Discord has a 2000-char limit per message
        if len(response.text) > 2000:
            payload["content"] = response.text[:1997] + "..."

        try:
            async with self._session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 201):
                    logger.info("[discord] Message sent to channel %s", channel_id)
                    return True
                body = await resp.text()
                logger.warning("[discord] Send failed (%s): %s", resp.status, body[:300])
                return False
        except Exception as exc:
            logger.error("[discord] Send error: %s", exc)
            return False

    # ── Raw message parsing ──────────────────────────────────────────────────

    def _parse_raw_message(self, raw_data: Dict[str, Any]) -> Optional[BotMessage]:
        """Extract the pre-built BotMessage from _on_message_create."""
        msg = raw_data.get("_message")
        if isinstance(msg, BotMessage):
            return msg
        return None
