"""Feishu (Lark) Stream Bot — long-lived WebSocket connection.

Feishu's Stream mode uses a WebSocket connection to receive events.
The bot authenticates with App ID + App Secret, opens a WS session
via the Lark/Feishu gateway, and processes incoming messages.

Configuration (env vars):
  FEISHU_APP_ID       — App ID (required)
  FEISHU_APP_SECRET   — App secret (required)

Requires: aiohttp.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import aiohttp

from .bot_platform import (
    BotMessage,
    BotPlatform,
    BotPlatformType,
    BotResponse,
)

logger = logging.getLogger(__name__)

# Feishu API endpoints
_FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_FEISHU_REPLY_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
_FEISHU_SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_FEISHU_WS_ENDPOINT = "wss://open.feishu.cn/event/ws"


class FeishuBot(BotPlatform):
    """Feishu (Lark) robot integration via Stream WebSocket mode."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        super().__init__(name="feishu", config=config)

        self._app_id: str = config.get("app_id") or os.getenv("FEISHU_APP_ID", "")
        self._app_secret: str = config.get("app_secret") or os.getenv("FEISHU_APP_SECRET", "")

        # Runtime state
        self._tenant_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

    @property
    def platform_type(self) -> BotPlatformType:
        return BotPlatformType.FEISHU

    # ── Token management ─────────────────────────────────────────────────────

    async def _ensure_token(self) -> str:
        """Get a valid tenant access token, refreshing if expired."""
        now = time.time()
        if self._tenant_token and now < self._token_expires_at - 60:
            token: str = self._tenant_token
            return token

        if not self._session:
            self._session = aiohttp.ClientSession()

        payload = {"app_id": self._app_id, "app_secret": self._app_secret}
        async with self._session.post(_FEISHU_TOKEN_URL, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Feishu token request failed ({resp.status}): {text}")
            data = await resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"Feishu token error: {data.get('msg', 'unknown')}")

        self._tenant_token = data["tenant_access_token"]
        self._token_expires_at = now + int(data.get("expire", 7200))
        logger.info("[feishu] Tenant token refreshed, expires in %ss", data.get("expire"))
        assert self._tenant_token is not None
        return self._tenant_token

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Open a WebSocket session with Feishu's event gateway."""
        if not self._app_id or not self._app_secret:
            raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET must be set")

        if not self._session:
            self._session = aiohttp.ClientSession()

        token = await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # Feishu's WebSocket endpoint for event subscription
        self._ws = await self._session.ws_connect(
            _FEISHU_WS_ENDPOINT,
            headers=headers,
            heartbeat=30,
        )
        logger.info("[feishu] WebSocket connected")

    async def _disconnect(self) -> None:
        """Close WebSocket and HTTP session."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._tenant_token = None

    async def _receive_messages(self) -> None:
        """Receive loop for Feishu Stream events."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._handle_event(data)
                except json.JSONDecodeError:
                    logger.warning("[feishu] Non-JSON WS message: %s", msg.data[:200])
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("[feishu] WS error: %s", self._ws.exception())
                break
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                logger.info("[feishu] WS closed")
                break

    async def _handle_event(self, data: Dict[str, Any]) -> None:
        """Process a Feishu event message.

        Feishu events have a top-level ``schema`` field (2.0) or are legacy format.
        We handle ``im.message.receive_v1`` (message received) events.
        """
        # Feishu 2.0 event format
        header = data.get("header", {})
        event_type = header.get("event_type", "")

        # URL verification challenge (initial setup)
        if "challenge" in data:
            logger.info("[feishu] Challenge received (setup verification)")
            # In stream mode, the challenge is handled by the gateway automatically
            return

        # Keep-alive / heartbeat
        if data.get("type") == "heartbeat":
            logger.debug("[feishu] Heartbeat")
            return

        if event_type != "im.message.receive_v1":
            logger.debug("[feishu] Ignored event: %s", event_type)
            return

        event = data.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})

        # Extract message content
        msg_type = message.get("message_type", "")
        if msg_type != "text":
            logger.debug("[feishu] Ignored message type: %s", msg_type)
            return

        # Content is JSON-encoded: {"text": "分析 AAPL"}
        content_str = message.get("content", "{}")
        try:
            content = json.loads(content_str)
            text = content.get("text", "").strip()
        except (json.JSONDecodeError, TypeError):
            text = content_str.strip()

        # Remove @mention prefix if present (e.g., "@_user_1 分析 AAPL")
        # Feishu uses @_user_N placeholders
        import re
        text = re.sub(r"^@_user_\d+\s*", "", text).strip()

        if not text:
            return

        sender_id = sender.get("sender_id", {})
        user_id = sender_id.get("open_id", "")
        user_name = sender.get("sender_type", "")
        chat_id = message.get("chat_id", "")
        message_id = message.get("message_id", "")

        bot_message = BotMessage(
            platform=BotPlatformType.FEISHU,
            text=text,
            user_id=user_id,
            user_name=user_name,
            chat_id=chat_id,
            message_id=message_id,
            extra={
                "chat_type": message.get("chat_type", ""),
                "message_type": msg_type,
            },
        )
        await self._handle_raw({"_message": bot_message})

    # ── Send response ────────────────────────────────────────────────────────

    async def _send_response(self, response: BotResponse) -> bool:
        """Send a reply via Feishu IM API."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        token = await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        # Reply to the original message if we have a message_id
        if response.message_id:
            url = _FEISHU_REPLY_URL.format(message_id=response.message_id)
            payload = {
                "content": json.dumps({"text": response.text}),
                "msg_type": "text",
            }
        else:
            # Send a new message to the chat
            url = _FEISHU_SEND_URL
            payload = {
                "receive_id": response.chat_id,
                "content": json.dumps({"text": response.text}),
                "msg_type": "text",
            }
            # Feishu requires a query param for the receive_id type
            url += "?receive_id_type=chat_id"

        try:
            async with self._session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    if body.get("code") == 0:
                        logger.info("[feishu] Message sent OK")
                        return True
                    logger.warning("[feishu] API error: %s", body.get("msg", "unknown"))
                    return False
                body_text = await resp.text()
                logger.warning("[feishu] Send failed (%s): %s", resp.status, body_text[:300])
                return False
        except Exception as exc:
            logger.error("[feishu] Send error: %s", exc)
            return False

    # ── Raw message parsing ──────────────────────────────────────────────────

    def _parse_raw_message(self, raw_data: Dict[str, Any]) -> Optional[BotMessage]:
        """Extract the pre-built BotMessage from _handle_event."""
        msg = raw_data.get("_message")
        if isinstance(msg, BotMessage):
            return msg
        return None
