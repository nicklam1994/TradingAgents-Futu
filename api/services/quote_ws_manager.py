"""WebSocket Quote Manager — Futu subscription with real-time callback push."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import WebSocket

from futu import (
    OpenQuoteContext,
    SecurityFirm,
    SubType,
    StockQuoteHandlerBase,
)

from tradingagents.dataflows.providers.futu_provider import (
    _opend_host,
    _opend_port,
    _canonical_to_futu,
)

logger = logging.getLogger(__name__)

MAX_SUBSCRIPTIONS = 300


class _QuoteHandler(StockQuoteHandlerBase):
    """Futu callback handler — receives real-time quote pushes."""

    def __init__(self, manager: "QuoteWSManager"):
        super().__init__()
        self._manager = manager

    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != 0 or data is None:
            return ret, data

        try:
            row = data.iloc[0] if hasattr(data, "iloc") else data
            futu_code = str(row.get("code", ""))
            canonical = self._manager._futu_to_canonical.get(futu_code, futu_code)

            last_price = float(row.get("last_price", 0) or 0)
            prev_close = float(row.get("prev_close_price", 0) or 0)
            change = last_price - prev_close if prev_close else 0
            change_pct = (change / prev_close * 100) if prev_close else 0

            quote = {
                "price": last_price,
                "change": round(change, 4),
                "change_pct": round(change_pct, 4),
                "volume": float(row.get("volume", 0) or 0),
                "turnover": float(row.get("turnover", 0) or 0),
                "high": float(row.get("high_price", 0) or 0),
                "low": float(row.get("low_price", 0) or 0),
                "open": float(row.get("open_price", 0) or 0),
                "prev_close": prev_close,
                "amplitude": float(row.get("amplitude", 0) or 0),
                "turnover_rate": float(row.get("turnover_rate", 0) or 0),
                "lot_size": int(row.get("lot_size", 0) or 0),
                "sec_status": str(row.get("sec_status", "") or ""),
            }

            # Schedule async broadcast via event loop
            self._manager._loop.call_soon_threadsafe(
                self._manager._schedule_update, canonical, quote
            )

        except Exception as exc:
            logger.warning("[quote-ws] callback error: %s", exc)

        return ret, data


class QuoteWSManager:
    """Manages WebSocket connections and Futu quote subscriptions via callbacks."""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._base_symbols: set[str] = set()      # Watchlist (primary)
        self._extra_symbols: set[str] = set()     # SimTrading, etc.
        self._subscribed_symbols: set[str] = set()  # Merged set
        self._latest_quotes: dict[str, dict[str, Any]] = {}
        self._latest_states: dict[str, str] = {}
        self._futu_to_canonical: dict[str, str] = {}
        self._ctx: OpenQuoteContext | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._state_refresh_task: asyncio.Task | None = None

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
        """Set base (watchlist) symbols — replaces base set only."""
        new_set = set(symbols)
        if new_set == self._base_symbols:
            return
        self._base_symbols = new_set
        self._resync_subscriptions()

    def add_symbols(self, symbols: list[str]):
        """Add extra symbols (SimTrading, etc.) — additive."""
        new_set = set(symbols)
        if new_set <= self._extra_symbols:
            return  # Already subscribed
        self._extra_symbols = new_set
        self._resync_subscriptions()

    def _resync_subscriptions(self):
        """Merge base + extra and re-subscribe if changed."""
        merged = self._base_symbols | self._extra_symbols
        if merged == self._subscribed_symbols:
            return
        self._subscribed_symbols = merged
        logger.info("[quote-ws] symbols resync: base=%d + extra=%d = total %d",
                     len(self._base_symbols), len(self._extra_symbols),
                     len(self._subscribed_symbols))
        if self._loop and self._running:
            self._loop.call_soon_threadsafe(self._schedule_resubscribe)

    def _schedule_update(self, canonical: str, quote: dict):
        """Called from handler thread via call_soon_threadsafe."""
        self._latest_quotes[canonical] = quote
        asyncio.ensure_future(self.broadcast({
            "type": "quote_update",
            "symbol": canonical,
            "data": quote,
            "ts": time.time(),
        }))

    def _schedule_resubscribe(self):
        """Schedule re-subscription."""
        asyncio.ensure_future(self._do_subscribe())

    async def start(self):
        """Start the Futu connection and handler."""
        if self._running:
            return
        self._loop = asyncio.get_event_loop()
        self._running = True
        # Start periodic market state refresh
        self._state_refresh_task = asyncio.create_task(self._periodic_state_refresh())
        logger.info("[quote-ws] Futu callback handler started")

    async def stop(self):
        """Clean up Futu connection."""
        self._running = False
        if self._state_refresh_task:
            self._state_refresh_task.cancel()
            try:
                await self._state_refresh_task
            except asyncio.CancelledError:
                pass
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None
        logger.info("[quote-ws] stopped")

    async def _periodic_state_refresh(self):
        """Periodically refresh market states and broadcast to clients."""
        while self._running:
            try:
                await asyncio.sleep(30)  # Refresh every 30 seconds
                if not self._subscribed_symbols or not self._ctx:
                    continue
                # Fetch latest market states
                futu_codes = list(self._futu_to_canonical.keys())
                if not futu_codes:
                    continue
                loop = asyncio.get_event_loop()
                ret, state_data = await loop.run_in_executor(
                    None, self._ctx.get_market_state, futu_codes
                )
                if ret == 0 and state_data is not None:
                    updated = False
                    for _, row in state_data.iterrows():
                        fc = row.get("code", "")
                        canonical = self._futu_to_canonical.get(fc, fc)
                        new_state = row.get("market_state", "")
                        if self._latest_states.get(canonical) != new_state:
                            self._latest_states[canonical] = new_state
                            updated = True
                    # Broadcast if any state changed
                    if updated and self._connections:
                        await self.broadcast({
                            "type": "states",
                            "states": self._latest_states,
                            "ts": time.time(),
                        })
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[quote-ws] state refresh error: %s", exc)

    async def _do_subscribe(self):
        """Subscribe to symbols via Futu context."""
        symbols = list(self._subscribed_symbols)
        if not symbols:
            return

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._subscribe_sync, symbols)
        except Exception as exc:
            logger.warning("[quote-ws] subscribe error: %s", exc)

    def _subscribe_sync(self, symbols: list[str]):
        """Synchronous Futu subscription (runs in executor)."""
        # Build futu code mapping
        futu_codes = []
        self._futu_to_canonical = {}
        for sym in symbols:
            fc = _canonical_to_futu(sym)
            futu_codes.append(fc)
            self._futu_to_canonical[fc] = sym

        # Create or reuse context
        if self._ctx is None:
            self._ctx = OpenQuoteContext(
                host=_opend_host(),
                port=_opend_port(),
                security_firm=SecurityFirm.FUTUSECURITIES,
            )
            self._ctx.set_handler(_QuoteHandler(self))

        # Subscribe
        ret, err = self._ctx.subscribe(futu_codes, [SubType.QUOTE], subscribe_push=True)
        if ret != 0:
            logger.warning("[quote-ws] subscribe failed: %s", err)
            return

        # Fetch initial snapshot + market state
        ret, data = self._ctx.get_stock_quote(futu_codes)
        if ret == 0 and data is not None:
            for _, row in data.iterrows():
                fc = row.get("code", "")
                canonical = self._futu_to_canonical.get(fc, fc)
                last_price = float(row.get("last_price", 0) or 0)
                prev_close = float(row.get("prev_close_price", 0) or 0)
                change = last_price - prev_close if prev_close else 0
                change_pct = (change / prev_close * 100) if prev_close else 0

                self._latest_quotes[canonical] = {
                    "price": last_price,
                    "change": round(change, 4),
                    "change_pct": round(change_pct, 4),
                    "volume": float(row.get("volume", 0) or 0),
                    "turnover": float(row.get("turnover", 0) or 0),
                    "high": float(row.get("high_price", 0) or 0),
                    "low": float(row.get("low_price", 0) or 0),
                    "open": float(row.get("open_price", 0) or 0),
                    "prev_close": prev_close,
                    "amplitude": float(row.get("amplitude", 0) or 0),
                    "turnover_rate": float(row.get("turnover_rate", 0) or 0),
                }

        # Market state
        ret2, state_data = self._ctx.get_market_state(futu_codes)
        if ret2 == 0 and state_data is not None:
            for _, row in state_data.iterrows():
                fc = row.get("code", "")
                canonical = self._futu_to_canonical.get(fc, fc)
                self._latest_states[canonical] = row.get("market_state", "")

        # Broadcast snapshot
        if self._loop and self._latest_quotes:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({
                    "type": "quotes",
                    "data": self._latest_quotes,
                    "states": self._latest_states,
                    "ts": time.time(),
                }),
                self._loop,
            )

        logger.info("[quote-ws] subscribed %d symbols, snapshot sent", len(futu_codes))


# Singleton
quote_ws_manager = QuoteWSManager()
