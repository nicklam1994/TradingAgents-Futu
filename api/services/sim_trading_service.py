"""Simulated Trading Service — Futu OpenD simulated account operations.

Wraps Futu OpenD's trading API (OpenSecTradeContext) for simulated trading
(TrdEnv.SIMULATE). Provides account info, positions, order management,
and automated signal-based execution.

Requires a running FutuOpenD instance (default: 127.0.0.1:11111).
Configure via FUTU_OPEND_HOST / FUTU_OPEND_PORT environment variables.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Configuration helpers ────────────────────────────────────────────────────

def _opend_host() -> str:
    """Read FutuOpenD host from env, default to localhost."""
    return os.getenv("FUTU_OPEND_HOST", "127.0.0.1")


def _opend_port() -> int:
    """Read FutuOpenD port from env, default to 11111."""
    return int(os.getenv("FUTU_OPEND_PORT", "11111"))


# ── Enums ────────────────────────────────────────────────────────────────────

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    NORMAL = "NORMAL"          # 限价单
    MARKET = "MARKET"          # 市价单（仅美股/期货）
    AUCTION_LIMIT = "AUCTION_LIMIT"  # 竞价限价单
    AUCTION_MARKET = "AUCTION_MARKET"  # 竞价市价单


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class AccountInfo:
    """Account financial information."""
    total_assets: float = 0.0       # 总资产
    cash_balance: float = 0.0       # 现金余额
    frozen_cash: float = 0.0        # 冻结资金
    market_val: float = 0.0         # 持仓市值
    currency: str = "HKD"
    available_cash: float = 0.0     # 可用资金（买入力）
    unrealized_pnl: float = 0.0     # 未实现盈亏
    realized_pnl: float = 0.0       # 已实现盈亏


@dataclass
class Position:
    """Single position entry."""
    code: str = ""                  # 股票代码 (e.g., "HK.00700", "US.AAPL")
    symbol: str = ""                # 原始符号
    qty: int = 0                    # 持仓数量
    cost_price: float = 0.0         # 成本价
    current_price: float = 0.0      # 现价
    market_val: float = 0.0         # 市值
    unrealized_pnl: float = 0.0     # 未实现盈亏
    unrealized_pnl_pct: float = 0.0 # 未实现盈亏百分比
    currency: str = "HKD"


@dataclass
class OrderResult:
    """Result of placing an order."""
    order_id: str = ""
    code: str = ""
    side: str = ""
    price: float = 0.0
    qty: float = 0.0
    status: str = ""
    create_time: str = ""


@dataclass
class OrderInfo:
    """Order detail."""
    order_id: str = ""
    code: str = ""
    stock_name: str = ""
    side: str = ""
    order_type: str = ""
    price: float = 0.0
    qty: float = 0.0
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    status: str = ""
    create_time: str = ""
    updated_time: str = ""
    currency: str = "HKD"


@dataclass
class DealInfo:
    """Executed deal detail."""
    order_id: str = ""
    deal_id: str = ""
    code: str = ""
    stock_name: str = ""
    side: str = ""
    price: float = 0.0
    qty: float = 0.0
    deal_time: str = ""
    currency: str = "HKD"


# ── Symbol conversion ────────────────────────────────────────────────────────

def _to_futu_trade_code(symbol: str) -> tuple:
    """Convert symbol to Futu (market, code) tuple for trading.

    Same logic as FutuProvider._to_futu_code but returns TrdMarket instead
    of Market for trading context.

    Rules:
      AAPL      → (TrdMarket.US, AAPL)
      NVDA.US   → (TrdMarket.US, NVDA)
      00700.HK  → (TrdMarket.HK, 00700)
      600519.SH → (TrdMarket.CN, 600519)  — simulated only
      000001.SZ → (TrdMarket.CN, 000001)  — simulated only
    """
    from futu import TrdMarket

    s = symbol.strip().upper()

    # Explicit HK suffix
    if s.endswith(".HK"):
        ticker = s[:-3]
        return (TrdMarket.HK, ticker)

    # Explicit US suffix
    if s.endswith(".US"):
        ticker = s[:-3]
        return (TrdMarket.US, ticker)

    # A-share suffixes — simulated trading supports CN market
    if s.endswith(".SH"):
        ticker = s[:-3]
        return (TrdMarket.CN, ticker)
    if s.endswith(".SZ"):
        ticker = s[:-3]
        return (TrdMarket.CN, ticker)

    # No suffix → assume US market
    return (TrdMarket.US, s)


def _symbol_from_futu_code(code: str) -> str:
    """Convert Futu code (e.g., 'HK.00700') back to our symbol format."""
    return code


# ── Trade context factory ────────────────────────────────────────────────────

def _get_trade_ctx(symbol: Optional[str] = None):
    """Create an OpenSecTradeContext for simulated trading.

    If symbol is provided, uses its market; otherwise defaults to HK.
    Caller MUST close the context when done (use try/finally pattern).
    """
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm

    if symbol:
        market, _ = _to_futu_trade_code(symbol)
    else:
        market = TrdMarket.HK

    return OpenSecTradeContext(
        filter_trdmarket=market,
        host=_opend_host(),
        port=_opend_port(),
        security_firm=SecurityFirm.FUTUSECURITIES,
    )


# ── Core service methods ────────────────────────────────────────────────────

def get_account(trd_env: str = "SIMULATE") -> AccountInfo:
    """Query simulated trading account financial information.

    Uses Futu accinfo_query to retrieve total assets, cash balance,
    frozen funds, and position market value.

    Args:
        trd_env: Trading environment. Only "SIMULATE" is supported.

    Returns:
        AccountInfo with all financial fields populated.

    Raises:
        RuntimeError: If FutuOpenD is offline or API call fails.
        ValueError: If trd_env is not "SIMULATE".
    """
    from futu import RET_OK, TrdEnv

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    # Use a general context — accinfo_query doesn't need a specific market
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.HK,
        host=_opend_host(),
        port=_opend_port(),
        security_firm=SecurityFirm.FUTUSECURITIES,
    )
    try:
        ret, data = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE)
        if ret != RET_OK:
            raise RuntimeError(f"Futu accinfo_query failed: {data}")

        if data is None or data.empty:
            raise RuntimeError("No account data returned from FutuOpenD")

        row = data.iloc[0]
        return AccountInfo(
            total_assets=float(row.get("total_assets", 0)),
            cash_balance=float(row.get("cash", 0)),
            frozen_cash=float(row.get("frozen_cash", 0)),
            market_val=float(row.get("market_val", 0)),
            currency=str(row.get("currency", "HKD")),
            available_cash=float(row.get("poweravl", row.get("cash", 0))),
            unrealized_pnl=float(row.get("unrealized_pnl", 0)),
            realized_pnl=float(row.get("realized_pnl", 0)),
        )
    except ImportError:
        raise RuntimeError(
            "futu package not installed. Install with: pip install futu-api"
        )
    except ConnectionError:
        raise RuntimeError(
            f"Cannot connect to FutuOpenD at {_opend_host()}:{_opend_port()}. "
            "Please ensure FutuOpenD is running."
        )
    finally:
        ctx.close()


def get_positions(
    trd_env: str = "SIMULATE",
    page_size: int = 100,
    page_index: int = 0,
) -> List[Position]:
    """Query simulated trading positions.

    Args:
        trd_env: Trading environment. Only "SIMULATE" is supported.
        page_size: Number of positions per page (max 1000).
        page_index: Page index (0-based).

    Returns:
        List of Position objects.

    Raises:
        RuntimeError: If FutuOpenD is offline or API call fails.
    """
    from futu import RET_OK, TrdEnv, OpenSecTradeContext, TrdMarket, SecurityFirm

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.HK,
        host=_opend_host(),
        port=_opend_port(),
        security_firm=SecurityFirm.FUTUSECURITIES,
    )
    try:
        ret, data = ctx.position_list_query(
            trd_env=TrdEnv.SIMULATE,
            page_index=page_index,
            page_size=min(page_size, 1000),
        )
        if ret != RET_OK:
            raise RuntimeError(f"Futu position_list_query failed: {data}")

        if data is None or data.empty:
            return []

        positions = []
        for _, row in data.iterrows():
            qty = int(row.get("qty", 0))
            if qty == 0:
                continue  # Skip zero-qty positions
            cost_price = float(row.get("cost_price", 0))
            current_price = float(row.get("nominal_price", row.get("last_price", 0)))
            market_val = float(row.get("market_val", 0))
            unrealized_pnl = float(row.get("pl_val", 0))
            pl_ratio = float(row.get("pl_ratio", 0))

            positions.append(Position(
                code=str(row.get("code", "")),
                symbol=_symbol_from_futu_code(str(row.get("code", ""))),
                qty=qty,
                cost_price=cost_price,
                current_price=current_price,
                market_val=market_val,
                unrealized_pnl=unrealized_pnl,
                unrealized_pnl_pct=round(pl_ratio * 100, 2) if pl_ratio else 0.0,
                currency=str(row.get("currency", "HKD")),
            ))

        return positions
    finally:
        ctx.close()


def place_order(
    symbol: str,
    side: str,
    quantity: float,
    price: float = 0,
    order_type: str = "NORMAL",
    trd_env: str = "SIMULATE",
    remark: Optional[str] = None,
) -> OrderResult:
    """Place a simulated trading order.

    Args:
        symbol: Stock symbol (e.g., "AAPL", "00700.HK", "NVDA.US").
        side: "BUY" or "SELL".
        quantity: Order quantity (must be positive integer).
        price: Order price. For market orders, pass 0.
        order_type: "NORMAL" (limit), "MARKET", "AUCTION_LIMIT", "AUCTION_MARKET".
        trd_env: Trading environment. Only "SIMULATE" is supported.
        remark: Optional remark for the order (max 64 bytes UTF-8).

    Returns:
        OrderResult with order_id, status, etc.

    Raises:
        RuntimeError: If FutuOpenD is offline or order placement fails.
        ValueError: If parameters are invalid.
    """
    from futu import RET_OK, TrdEnv, TrdSide, ModifyOrderOp, SecurityFirm
    from futu import OpenSecTradeContext

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")

    # Convert side
    side_upper = side.strip().upper()
    if side_upper == "BUY":
        futu_side = TrdSide.BUY
    elif side_upper == "SELL":
        futu_side = TrdSide.SELL
    else:
        raise ValueError(f"Invalid side: {side}. Must be BUY or SELL.")

    # Convert order type
    order_type_upper = order_type.strip().upper()
    futu_order_type = _resolve_order_type(order_type_upper)

    # Get market-specific trade context
    market, code = _to_futu_trade_code(symbol)
    ctx = OpenSecTradeContext(
        filter_trdmarket=market,
        host=_opend_host(),
        port=_opend_port(),
        security_firm=SecurityFirm.FUTUSECURITIES,
    )
    try:
        ret, data = ctx.place_order(
            price=price,
            qty=quantity,
            code=code,
            trd_side=futu_side,
            order_type=futu_order_type,
            trd_env=TrdEnv.SIMULATE,
            remark=remark or "",
        )
        if ret != RET_OK:
            raise RuntimeError(f"Futu place_order failed for {symbol}: {data}")

        if data is None or data.empty:
            raise RuntimeError("place_order returned empty data")

        row = data.iloc[0]
        return OrderResult(
            order_id=str(row.get("order_id", "")),
            code=str(row.get("code", code)),
            side=side_upper,
            price=float(row.get("price", price)),
            qty=float(row.get("qty", quantity)),
            status=str(row.get("order_status", "SUBMITTED")),
            create_time=str(row.get("create_time", "")),
        )
    finally:
        ctx.close()


def cancel_order(
    order_id: str,
    symbol: str,
    trd_env: str = "SIMULATE",
) -> bool:
    """Cancel a pending simulated order.

    Args:
        order_id: The order ID to cancel.
        symbol: Stock symbol (needed to determine market context).
        trd_env: Trading environment. Only "SIMULATE" is supported.

    Returns:
        True if cancellation was successful.

    Raises:
        RuntimeError: If cancellation fails.
    """
    from futu import RET_OK, TrdEnv, ModifyOrderOp, SecurityFirm
    from futu import OpenSecTradeContext

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    market, _ = _to_futu_trade_code(symbol)
    ctx = OpenSecTradeContext(
        filter_trdmarket=market,
        host=_opend_host(),
        port=_opend_port(),
        security_firm=SecurityFirm.FUTUSECURITIES,
    )
    try:
        ret, data = ctx.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=TrdEnv.SIMULATE,
        )
        if ret != RET_OK:
            raise RuntimeError(f"Futu cancel_order failed for {order_id}: {data}")

        return True
    finally:
        ctx.close()


def get_orders(
    symbol: Optional[str] = None,
    trd_env: str = "SIMULATE",
    page_size: int = 100,
    page_index: int = 0,
) -> List[OrderInfo]:
    """Query simulated trading orders.

    Args:
        symbol: Optional stock symbol filter. If None, queries all markets.
        trd_env: Trading environment. Only "SIMULATE" is supported.
        page_size: Number of orders per page (max 1000).
        page_index: Page index (0-based).

    Returns:
        List of OrderInfo objects.
    """
    from futu import RET_OK, TrdEnv, OpenSecTradeContext, TrdMarket, SecurityFirm

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    if symbol:
        market, code = _to_futu_trade_code(symbol)
        ctx = OpenSecTradeContext(
            filter_trdmarket=market,
            host=_opend_host(),
            port=_opend_port(),
            security_firm=SecurityFirm.FUTUSECURITIES,
        )
        code_filter = code
    else:
        # Default to HK market if no symbol specified
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.HK,
            host=_opend_host(),
            port=_opend_port(),
            security_firm=SecurityFirm.FUTUSECURITIES,
        )
        code_filter = None

    try:
        ret, data = ctx.order_list_query(
            trd_env=TrdEnv.SIMULATE,
            page_index=page_index,
            page_size=min(page_size, 1000),
        )
        if ret != RET_OK:
            raise RuntimeError(f"Futu order_list_query failed: {data}")

        if data is None or data.empty:
            return []

        orders = []
        for _, row in data.iterrows():
            order = OrderInfo(
                order_id=str(row.get("order_id", "")),
                code=str(row.get("code", "")),
                stock_name=str(row.get("stock_name", "")),
                side=str(row.get("trd_side", "")),
                order_type=str(row.get("order_type", "")),
                price=float(row.get("price", 0)),
                qty=float(row.get("qty", 0)),
                filled_qty=float(row.get("dealt_qty", 0)),
                filled_avg_price=float(row.get("dealt_avg_price", 0)),
                status=str(row.get("order_status", "")),
                create_time=str(row.get("create_time", "")),
                updated_time=str(row.get("updated_time", "")),
                currency=str(row.get("currency", "HKD")),
            )
            # Filter by code if specified
            if code_filter and order.code != _symbol_from_futu_code(code_filter):
                continue
            orders.append(order)

        return orders
    finally:
        ctx.close()


def get_deals(
    symbol: Optional[str] = None,
    trd_env: str = "SIMULATE",
    page_size: int = 100,
    page_index: int = 0,
) -> List[DealInfo]:
    """Query today's executed deals (filled trades).

    Args:
        symbol: Optional stock symbol filter.
        trd_env: Trading environment. Only "SIMULATE" is supported.
        page_size: Number of deals per page (max 1000).
        page_index: Page index (0-based).

    Returns:
        List of DealInfo objects.
    """
    from futu import RET_OK, TrdEnv, OpenSecTradeContext, TrdMarket, SecurityFirm

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    if symbol:
        market, code = _to_futu_trade_code(symbol)
        ctx = OpenSecTradeContext(
            filter_trdmarket=market,
            host=_opend_host(),
            port=_opend_port(),
            security_firm=SecurityFirm.FUTUSECURITIES,
        )
    else:
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.HK,
            host=_opend_host(),
            port=_opend_port(),
            security_firm=SecurityFirm.FUTUSECURITIES,
        )

    try:
        ret, data = ctx.deal_list_query(
            trd_env=TrdEnv.SIMULATE,
            page_index=page_index,
            page_size=min(page_size, 1000),
        )
        if ret != RET_OK:
            raise RuntimeError(f"Futu deal_list_query failed: {data}")

        if data is None or data.empty:
            return []

        deals = []
        for _, row in data.iterrows():
            deals.append(DealInfo(
                order_id=str(row.get("order_id", "")),
                deal_id=str(row.get("deal_id", "")),
                code=str(row.get("code", "")),
                stock_name=str(row.get("stock_name", "")),
                side=str(row.get("trd_side", "")),
                price=float(row.get("price", 0)),
                qty=float(row.get("qty", 0)),
                deal_time=str(row.get("create_time", "")),
                currency=str(row.get("currency", "HKD")),
            ))

        return deals
    finally:
        ctx.close()


# ── Signal execution ─────────────────────────────────────────────────────────

@dataclass
class SignalInput:
    """Input for automated signal-based order execution."""
    symbol: str
    signal: str              # "buy", "sell", "hold"
    confidence: float        # 0.0 - 1.0
    target_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    max_position_pct: float = 0.25  # Max position as % of total assets
    use_kelly: bool = False         # Use Kelly criterion for sizing


@dataclass
class SignalResult:
    """Result of signal execution."""
    action_taken: str = "none"      # "buy", "sell", "hold", "skipped"
    order_id: Optional[str] = None
    symbol: str = ""
    side: str = ""
    quantity: int = 0
    price: float = 0.0
    reason: str = ""
    kelly_fraction: Optional[float] = None


def execute_signal(signal: SignalInput) -> SignalResult:
    """Execute a trading signal automatically.

    Takes an Agent analysis signal and converts it to a simulated order.

    Risk controls:
    - confidence < 0.5 → no order (skip)
    - signal == "hold" → no order
    - Position size capped by max_position_pct of total assets
    - Optional Kelly criterion for position sizing

    Args:
        signal: SignalInput with symbol, signal, confidence, etc.

    Returns:
        SignalResult describing what action was taken.
    """
    from futu import RET_OK, TrdEnv

    # ── Gate: confidence check ──
    if signal.confidence < 0.5:
        return SignalResult(
            action_taken="skipped",
            symbol=signal.symbol,
            reason=f"Confidence {signal.confidence:.2f} < 0.5 threshold, skipping",
        )

    # ── Gate: hold signal ──
    signal_lower = signal.signal.strip().lower()
    if signal_lower == "hold":
        return SignalResult(
            action_taken="hold",
            symbol=signal.symbol,
            reason="Signal is HOLD, no order placed",
        )

    if signal_lower not in ("buy", "sell"):
        return SignalResult(
            action_taken="skipped",
            symbol=signal.symbol,
            reason=f"Unknown signal '{signal.signal}', expected buy/sell/hold",
        )

    # ── Get account info for position sizing ──
    try:
        account = get_account("SIMULATE")
    except Exception as e:
        return SignalResult(
            action_taken="skipped",
            symbol=signal.symbol,
            reason=f"Failed to query account: {e}",
        )

    if account.total_assets <= 0:
        return SignalResult(
            action_taken="skipped",
            symbol=signal.symbol,
            reason="Account total assets is zero or negative",
        )

    # ── Determine order price ──
    price = signal.target_price
    if price is None or price <= 0:
        # Fetch current price from market data
        try:
            from tradingagents.dataflows.providers.futu_provider import FutuProvider
            provider = FutuProvider()
            quotes_csv = provider.get_realtime_quotes([signal.symbol])
            if quotes_csv:
                import csv
                from io import StringIO
                reader = csv.DictReader(StringIO(quotes_csv))
                for row in reader:
                    price = float(row.get("price", 0))
                    break
        except Exception:
            pass

    if not price or price <= 0:
        return SignalResult(
            action_taken="skipped",
            symbol=signal.symbol,
            reason="Could not determine order price (no target_price and quote fetch failed)",
        )

    # ── Position sizing ──
    if signal.use_kelly:
        # Kelly criterion: f* = (p * b - q) / b
        # where p = win probability (confidence), q = 1-p, b = odds ratio
        # Assume symmetric odds (b=1) for simplicity: f* = 2p - 1
        kelly = max(0, 2 * signal.confidence - 1)
        max_alloc = account.total_assets * kelly * 0.5  # Half-Kelly for safety
    else:
        max_alloc = account.total_assets * signal.max_position_pct

    # For SELL, check existing position
    if signal_lower == "sell":
        try:
            positions = get_positions("SIMULATE")
            existing = [p for p in positions if p.symbol.upper() == signal.symbol.upper()
                        or p.code.upper() == signal.symbol.upper()]
            if not existing:
                return SignalResult(
                    action_taken="skipped",
                    symbol=signal.symbol,
                    reason=f"No existing position to sell for {signal.symbol}",
                )
            # Sell all of existing position
            quantity = existing[0].qty
        except Exception as e:
            return SignalResult(
                action_taken="skipped",
                symbol=signal.symbol,
                reason=f"Failed to query positions for sell: {e}",
            )
    else:
        # BUY: calculate quantity from allocation
        quantity = int(max_alloc / price)
        if quantity <= 0:
            return SignalResult(
                action_taken="skipped",
                symbol=signal.symbol,
                reason=f"Calculated quantity is 0 (max_alloc={max_alloc:.2f}, price={price:.2f})",
            )

    # ── Place the order ──
    side = signal_lower.upper()
    try:
        result = place_order(
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            price=price,
            order_type="NORMAL",
            trd_env="SIMULATE",
            remark=f"auto-signal conf={signal.confidence:.2f}",
        )
        return SignalResult(
            action_taken=signal_lower,
            order_id=result.order_id,
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            price=price,
            reason=f"Order placed: {side} {quantity} @ {price}",
            kelly_fraction=(2 * signal.confidence - 1) if signal.use_kelly else None,
        )
    except Exception as e:
        return SignalResult(
            action_taken="skipped",
            symbol=signal.symbol,
            reason=f"Order placement failed: {e}",
        )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _resolve_order_type(order_type_str: str):
    """Convert string order type to Futu OrderType enum."""
    from futu import OrderType

    mapping = {
        "NORMAL": OrderType.NORMAL,
        "MARKET": OrderType.MARKET,
        "AUCTION_LIMIT": OrderType.AUCTION_LIMIT,
        "AUCTION_MARKET": OrderType.AUCTION_MARKET,
    }
    return mapping.get(order_type_str, OrderType.NORMAL)


# ── Serialization helpers ────────────────────────────────────────────────────

def account_to_dict(info: AccountInfo) -> Dict[str, Any]:
    """Serialize AccountInfo to dict for API response."""
    return {
        "total_assets": info.total_assets,
        "cash_balance": info.cash_balance,
        "frozen_cash": info.frozen_cash,
        "market_val": info.market_val,
        "currency": info.currency,
        "available_cash": info.available_cash,
        "unrealized_pnl": info.unrealized_pnl,
        "realized_pnl": info.realized_pnl,
    }


def position_to_dict(p: Position) -> Dict[str, Any]:
    """Serialize Position to dict for API response."""
    return {
        "code": p.code,
        "symbol": p.symbol,
        "qty": p.qty,
        "cost_price": p.cost_price,
        "current_price": p.current_price,
        "market_val": p.market_val,
        "unrealized_pnl": p.unrealized_pnl,
        "unrealized_pnl_pct": p.unrealized_pnl_pct,
        "currency": p.currency,
    }


def order_result_to_dict(r: OrderResult) -> Dict[str, Any]:
    """Serialize OrderResult to dict for API response."""
    return {
        "order_id": r.order_id,
        "code": r.code,
        "side": r.side,
        "price": r.price,
        "qty": r.qty,
        "status": r.status,
        "create_time": r.create_time,
    }


def order_info_to_dict(o: OrderInfo) -> Dict[str, Any]:
    """Serialize OrderInfo to dict for API response."""
    return {
        "order_id": o.order_id,
        "code": o.code,
        "stock_name": o.stock_name,
        "side": o.side,
        "order_type": o.order_type,
        "price": o.price,
        "qty": o.qty,
        "filled_qty": o.filled_qty,
        "filled_avg_price": o.filled_avg_price,
        "status": o.status,
        "create_time": o.create_time,
        "updated_time": o.updated_time,
        "currency": o.currency,
    }


def deal_info_to_dict(d: DealInfo) -> Dict[str, Any]:
    """Serialize DealInfo to dict for API response."""
    return {
        "order_id": d.order_id,
        "deal_id": d.deal_id,
        "code": d.code,
        "stock_name": d.stock_name,
        "side": d.side,
        "price": d.price,
        "qty": d.qty,
        "deal_time": d.deal_time,
        "currency": d.currency,
    }


def signal_result_to_dict(r: SignalResult) -> Dict[str, Any]:
    """Serialize SignalResult to dict for API response."""
    return {
        "action_taken": r.action_taken,
        "order_id": r.order_id,
        "symbol": r.symbol,
        "side": r.side,
        "quantity": r.quantity,
        "price": r.price,
        "reason": r.reason,
        "kelly_fraction": r.kelly_fraction,
    }
