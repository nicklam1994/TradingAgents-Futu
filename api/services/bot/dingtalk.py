"""DingTalk Bot — Stream (WebSocket) and Webhook modes.

Supports two connection modes:
  1. **Stream mode** (default): Persistent WebSocket connection to DingTalk's
     gateway.  The bot registers with DingTalk's dispatch endpoint and receives
     messages over a long-lived WS session.
  2. **Webhook mode**: FastAPI endpoint receives POST callbacks from DingTalk.
     Replies are sent via the OpenAPI ``/v1.0/robot/oToMessages/batchSend``.

Configuration (env vars):
  DINGTALK_APP_KEY        — App key (required)
  DINGTALK_APP_SECRET     — App secret (required)
  DINGTALK_ROBOT_CODE     — Robot code for sending messages (stream mode)
  DINGTALK_MODE           — "stream" (default) or "webhook"

Requires: aiohttp (already a transitive dependency via langchain).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
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

# DingTalk API endpoints
_DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_DINGTALK_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
_DINGTALK_SEND_OTO_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
_DINGTALK_SEND_GROUP_URL = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
_DINGTALK_STREAM_REGISTER_URL = "https://api.dingtalk.com/v1.0/gateway/connections/open"


class DingTalkBot(BotPlatform):
    """DingTalk robot integration via Stream or Webhook mode."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        super().__init__(name="dingtalk", config=config)

        # Credentials from config or env
        self._app_key: str = config.get("app_key") or os.getenv("DINGTALK_APP_KEY", "")
        self._app_secret: str = config.get("app_secret") or os.getenv("DINGTALK_APP_SECRET", "")
        self._robot_code: str = config.get("robot_code") or os.getenv("DINGTALK_ROBOT_CODE", "")
        self._mode: str = config.get("mode") or os.getenv("DINGTALK_MODE", "stream")

        # Runtime state
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

    @property
    def platform_type(self) -> BotPlatformType:
        return BotPlatformType.DINGTALK

    # ── Token management ─────────────────────────────────────────────────────

    async def _ensure_token(self) -> str:
        """Get a valid access token, refreshing if expired."""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            token: str = self._access_token
            return token

        if not self._session:
            self._session = aiohttp.ClientSession()

        payload = {"appKey": self._app_key, "appSecret": self._app_secret}
        async with self._session.post(_DINGTALK_TOKEN_URL, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"DingTalk token request failed ({resp.status}): {text}")
            data = await resp.json()

        self._access_token = data["accessToken"]
        # expiresIn is in seconds
        self._token_expires_at = now + int(data.get("expireIn", 7200))
        logger.info("[dingtalk] Access token refreshed, expires in %ss", data.get("expireIn"))
        return self._access_token

    # ── Lifecycle (Stream mode) ──────────────────────────────────────────────

    async def _connect(self) -> None:
        """Establish connection.  Stream mode opens WS; Webhook mode is a no-op."""
        if not self._app_key or not self._app_secret:
            raise RuntimeError("DINGTALK_APP_KEY and DINGTALK_APP_SECRET must be set")

        if not self._session:
            self._session = aiohttp.ClientSession()

        if self._mode == "stream":
            await self._connect_stream()
        else:
            logger.info("[dingtalk] Webhook mode — waiting for HTTP callbacks")

    async def _connect_stream(self) -> None:
        """Open a WebSocket session with DingTalk's gateway."""
        token = await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "x-acs-action": "EstablishWebSocketConnection",
        }
        # DingTalk's stream endpoint
        ws_url = f"wss://api.dingtalk.com/v1.0/gateway/ws/open?access_token={token}"

        assert self._session is not None, "HTTP session must be initialized"
        self._ws = await self._session.ws_connect(ws_url, headers=headers, heartbeat=30)
        logger.info("[dingtalk] WebSocket connected")

    async def _disconnect(self) -> None:
        """Close WebSocket and HTTP session."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._access_token = None

    async def _receive_messages(self) -> None:
        """Receive loop for Stream mode."""
        if self._mode != "stream" or not self._ws:
            # Webhook mode: sleep indefinitely (messages come via HTTP)
            while self._running:
                await asyncio.sleep(3600)
            return

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._handle_stream_message(data)
                except json.JSONDecodeError:
                    logger.warning("[dingtalk] Non-JSON WS message: %s", msg.data[:200])
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("[dingtalk] WS error: %s", self._ws.exception())
                break
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                logger.info("[dingtalk] WS closed")
                break

    async def _handle_stream_message(self, data: Dict[str, Any]) -> None:
        """Process a DingTalk Stream message.

        DingTalk stream messages arrive as JSON with a top-level ``type`` field.
        Common types: "EVENT_CALLBACK", "SYSTEM", "KEEP_ALIVE".
        """
        msg_type = data.get("type", "")

        if msg_type == "KEEP_ALIVE":
            logger.debug("[dingtalk] Keep-alive received")
            return

        if msg_type == "SYSTEM":
            logger.info("[dingtalk] System message: %s", data.get("message", ""))
            return

        if msg_type == "EVENT_CALLBACK":
            event_data = data.get("data", {})
            # DingTalk wraps the actual event in a "data" field
            # The event header contains eventType
            header = event_data.get("header", {})
            event_type = header.get("eventType", "")
            event_body = event_data.get("data", {})

            if event_type == "ReceiveMsg":
                await self._handle_receive_msg(event_body, header)
            elif event_type == "ChatReceiveMessage":
                await self._handle_receive_msg(event_body, header)
            else:
                logger.debug("[dingtalk] Ignored event type: %s", event_type)

    async def _handle_receive_msg(self, body: Dict[str, Any], header: Dict[str, Any]) -> None:
        """Handle a ReceiveMsg event from DingTalk Stream."""
        # Extract message content
        msg_content = body.get("msgContent", "")
        # msgContent may be JSON-encoded string like '{"content":"分析 AAPL"}'
        try:
            parsed = json.loads(msg_content)
            text = parsed.get("content", msg_content)
        except (json.JSONDecodeError, TypeError):
            text = str(msg_content)

        sender_id = body.get("senderStaffId") or body.get("senderId", "")
        sender_nick = body.get("senderNick", "")
        conversation_id = body.get("conversationId", "")
        msg_id = body.get("msgId", "")

        message = BotMessage(
            platform=BotPlatformType.DINGTALK,
            text=text.strip(),
            user_id=sender_id,
            user_name=sender_nick,
            chat_id=conversation_id,
            message_id=msg_id,
            extra={
                "conversation_type": body.get("conversationType", ""),
                "robot_code": self._robot_code,
                "sender_staff_id": body.get("senderStaffId", ""),
            },
        )
        await self._handle_raw({"_message": message})

    # ── Webhook mode (HTTP callback from FastAPI) ────────────────────────────

    async def handle_webhook_callback(self, payload: Dict[str, Any]) -> Optional[BotResponse]:
        """Process an incoming DingTalk webhook callback.

        Called from the FastAPI endpoint when DingTalk POSTs a message.
        Returns the BotResponse to send back (or None).
        """
        text = payload.get("text", {}).get("content", "").strip()
        sender_id = payload.get("senderStaffId") or payload.get("senderId", "")
        sender_nick = payload.get("senderNick", "")
        conversation_id = payload.get("conversationId", "")

        message = BotMessage(
            platform=BotPlatformType.DINGTALK,
            text=text,
            user_id=sender_id,
            user_name=sender_nick,
            chat_id=conversation_id,
            extra=payload,
        )
        if self._handler:
            return await self._handler(message)
        return None

    # ── Send response ────────────────────────────────────────────────────────

    async def _send_response(self, response: BotResponse) -> bool:
        """Send a reply via DingTalk OpenAPI."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        token = await self._ensure_token()

        # For stream mode with robot_code, use the single-msg send API
        # For webhook mode, return the text for the webhook handler to reply
        if self._mode == "webhook":
            # In webhook mode the FastAPI handler returns the text directly
            logger.debug("[dingtalk] Webhook mode: response queued for HTTP return")
            return True

        # Stream mode: send via OpenAPI
        headers = {
            "x-acs-action": "RobotSendMessageToConversation",
            "x-acs-date": "",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # DingTalk single-chat message send
        # Use the conversation-based API
        send_payload = {
            "robotCode": self._robot_code,
            "senderUserId": response.extra.get("sender_user_id", ""),
            "msgKey": "sampleText",
            "msgParam": json.dumps({"content": response.text}),
        }

        # For conversation-based reply
        if response.chat_id:
            send_url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            send_payload["openConversationId"] = response.chat_id
        else:
            # OTo (one-to-one) message
            send_url = _DINGTALK_SEND_URL
            send_payload["userIds"] = [response.extra.get("sender_user_id", "")]

        try:
            async with self._session.post(send_url, json=send_payload, headers=headers) as resp:
                if resp.status == 200:
                    logger.info("[dingtalk] Message sent OK")
                    return True
                body = await resp.text()
                logger.warning("[dingtalk] Send failed (%s): %s", resp.status, body[:300])
                return False
        except Exception as exc:
            logger.error("[dingtalk] Send error: %s", exc)
            return False

    # ── Raw message parsing (Stream mode uses pre-parsed BotMessage) ─────────

    def _parse_raw_message(self, raw_data: Dict[str, Any]) -> Optional[BotMessage]:
        """Extract the pre-built BotMessage from _handle_receive_msg."""
        msg = raw_data.get("_message")
        if isinstance(msg, BotMessage):
            return msg
        return None
