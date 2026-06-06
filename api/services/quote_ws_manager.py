"""WebSocket Quote Manager — Futu subscription + real-time push to clients."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Futu subscription quota limit
MAX_SUBSCRIPTIONS = 300

# Polling interval for Futu quote updates (seconds)
POLL_INTERVAL = 2.0


class QuoteWSManager:
    """Manages WebSocket connections and Futu quote subscriptions."""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}  # user_id -> ws
        self._subscribed_symbols: set[str] = set()
        self._latest_quotes: dict[str, dict[str, Any]] = {}
        self._latest_states: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def subscription_count(self) -> int:
        return len(self._subscribed_symbols)

    @property
    def latest_quotes(self) -> dict[str, dict[str, Any]]:
        return self._latest_quotes.copy()

    @property
    def latest_states(self) -> dict[str, str]:
        return self._latest_states.copy()

    async def connect(self, user_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[user_id] = ws
        logger.info("[quote-ws] client connected: %s (total: %d)", user_id, len(self._connections))
        # Send current state immediately
        if self._latest_quotes:
            await self._send_to(ws, {
                "type": "quotes",
                "data": self._latest_quotes,
                "states": self._latest_states,
            })

    def disconnect(self, user_id: str):
        self._connections.pop(user_id, None)
        logger.info("[quote-ws] client disconnected: %s (total: %d)", user_id, len(self._connections))

    async def _send_to(self, ws: WebSocket, data: dict):
        try:
            await ws.send_json(data)
        except Exception:
            pass

    async def broadcast(self, data: dict):
        dead = []
        for uid, ws in self._connections.items():
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(uid)
        for uid in dead:
            self._connections.pop(uid, None)

    def update_symbols(self, symbols: list[str]):
        """Update the set of symbols to subscribe to."""
        new_set = set(symbols)
        if new_set != self._subscribed_symbols:
            self._subscribed_symbols = new_set
            logger.info("[quote-ws] symbols updated: %d subscribed", len(self._subscribed_symbols))

    async def start_polling(self):
        """Start the background polling task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("[quote-ws] polling started")

    async def stop_polling(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _poll_loop(self):
        """Poll Futu for quotes and broadcast to WebSocket clients."""
        while self._running:
            try:
                if self._subscribed_symbols and self._connections:
                    await self._fetch_and_broadcast()
            except Exception as exc:
                logger.warning("[quote-ws] poll error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    async def _fetch_and_broadcast(self):
        """Fetch quotes from Futu and broadcast to all clients."""
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self._fetch_quotes_sync)
            if result:
                self._latest_quotes = result["quotes"]
                self._latest_states = result["states"]
                await self.broadcast({
                    "type": "quotes",
                    "data": self._latest_quotes,
                    "states": self._latest_states,
                    "ts": time.time(),
                })
        except Exception as exc:
            logger.warning("[quote-ws] fetch error: %s", exc)

    def _fetch_quotes_sync(self) -> dict[str, Any] | None:
        """Synchronous Futu quote fetch (runs in executor)."""
        from futu import OpenQuoteContext, SecurityFirm, SubType
        from tradingagents.dataflows.providers.futu_provider import _opend_host, _opend_port, _canonical_to_futu

        symbols = list(self._subscribed_symbols)
        if not symbols:
            return None

        ctx = None
        try:
            ctx = OpenQuoteContext(
                host=_opend_host(),
                port=_opend_port(),
                security_firm=SecurityFirm.FUTUSECURITIES,
            )

            # Build futu code mapping
            futu_codes = []
            futu_to_canonical = {}
            for sym in symbols:
                fc = _canonical_to_futu(sym)
                futu_codes.append(fc)
                futu_to_canonical[fc] = sym

            # Subscribe to QUOTE type
            ret, err = ctx.subscribe(futu_codes, [SubType.QUOTE], subscribe_push=False)
            if ret != 0:
                logger.warning("[quote-ws] subscribe failed: %s", err)
                return None

            # Get subscription info for quota
            ret, sub_info = ctx.query_subscription()
            sub_quota = {}
            if ret == 0:
                sub_quota = {
                    "total_used": sub_info.get("total_used", 0),
                    "remain": sub_info.get("remain", 0),
                }

            # Fetch quotes
            ret, quote_data = ctx.get_stock_quote(futu_codes)
            if ret != 0:
                return None

            # Fetch market state
            ret2, state_data = ctx.get_market_state(futu_codes)
            states = {}
            if ret2 == 0 and state_data is not None:
                for _, row in state_data.iterrows():
                    fc = row.get("code", "")
                    canonical = futu_to_canonical.get(fc, fc)
                    states[canonical] = row.get("market_state", "")

            # Build quotes dict
            quotes = {}
            for _, row in quote_data.iterrows():
                fc = row.get("code", "")
                canonical = futu_to_canonical.get(fc, fc)
                last_price = row.get("last_price", 0)
                prev_close = row.get("prev_close_price", 0)
                change = last_price - prev_close if prev_close else 0
                change_pct = (change / prev_close * 100) if prev_close else 0

                quotes[canonical] = {
                    "price": last_price,
                    "change": round(change, 4),
                    "change_pct": round(change_pct, 4),
                    "volume": row.get("volume", 0),
                    "turnover": row.get("turnover", 0),
                    "high": row.get("high_price", 0),
                    "low": row.get("low_price", 0),
                    "open": row.get("open_price", 0),
                    "prev_close": prev_close,
                    "amplitude": row.get("amplitude", 0),
                    "turnover_rate": row.get("turnover_rate", 0),
                }

            return {"quotes": quotes, "states": states, "quota": sub_quota}

        except Exception as exc:
            logger.warning("[quote-ws] fetch error: %s", exc)
            return None
        finally:
            if ctx:
                ctx.close()


# Singleton
quote_ws_manager = QuoteWSManager()
