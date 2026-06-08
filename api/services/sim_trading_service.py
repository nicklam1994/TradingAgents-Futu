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
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Configuration helpers ────────────────────────────────────────────────────

def _opend_host() -> str:
    """Read FutuOpenD host from env or .env file, default to localhost."""
    host = os.getenv("FUTU_OPEND_HOST")
    if host:
        return host
    try:
        from dotenv import dotenv_values
        env_vals = dotenv_values(".env")
        host = env_vals.get("FUTU_OPEND_HOST")
        if host:
            return host
    except Exception:
        pass
    return "127.0.0.1"


def _opend_port() -> int:
    """Read FutuOpenD port from env, default to 11111."""
    return int(os.getenv("FUTU_OPEND_PORT", "11111"))


# ── RSA Encryption Setup ─────────────────────────────────────────────────────


def _get_rsa_path() -> Optional[str]:
    """Get the absolute path to the RSA key file, or None if not found."""
    rsa_path = os.getenv("FUTU_RSA_KEY_PATH", "config/rsa_key.txt")
    if not os.path.isabs(rsa_path):
        rsa_path = os.path.join(os.path.dirname(__file__), "..", "..", rsa_path)
    rsa_path = os.path.abspath(rsa_path)
    if not os.path.exists(rsa_path):
        logger.warning("RSA key not found at %s — trade connections may fail", rsa_path)
        return None
    return rsa_path


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
    market: str = "HK"              # 市场 (HK/US)
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
    stock_name: str = ""            # 股票名称
    qty: int = 0                    # 持仓数量
    cost_price: float = 0.0         # 成本价
    current_price: float = 0.0      # 现价
    prev_close: float = 0.0         # 昨收价
    market_val: float = 0.0         # 市值
    cost_val: float = 0.0           # 成本市值
    unrealized_pnl: float = 0.0     # 持仓盈亏
    unrealized_pnl_pct: float = 0.0 # 持仓盈亏%
    today_pnl: float = 0.0          # 今日盈亏
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
    deal_id: str = ""
    code: str = ""
    stock_name: str = ""
    side: str = ""
    deal_market: str = ""
    order_type: str = ""
    qty: float = 0.0
    price: float = 0.0
    create_time: str = ""
    status: str = ""
    currency: str = "HKD"


def _safe_float(val, default=0.0) -> float:
    """Convert value to float, handling 'N/A' and other non-numeric strings."""
    if val is None or val == "N/A" or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ── Local deal recording ─────────────────────────────────────────────────────

def _record_deal(
    order_id: str,
    code: str,
    stock_name: str,
    trd_side: str,
    deal_market: str,
    qty: float,
    price: float,
    order_type: str = "NORMAL",
    currency: str = "HKD",
) -> None:
    """Record a simulated deal in the local DB."""
    from api.database import SessionLocal, SimDealDB
    db = SessionLocal()
    try:
        deal = SimDealDB(
            id=uuid4().hex,
            order_id=str(order_id),
            deal_id=f"LOCAL-{uuid4().hex[:12]}",
            code=code,
            stock_name=stock_name,
            trd_side=trd_side,
            deal_market=deal_market,
            order_type=order_type,
            qty=qty,
            price=price,
            create_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            status="FILLED",
            currency=currency,
        )
        db.add(deal)
        db.commit()
    except Exception as exc:
        db.rollback()
        import logging
        logging.getLogger(__name__).warning("[sim-deal] failed to record deal: %s", exc)
    finally:
        db.close()


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

    # Futu prefix format: HK.00700, US.AAPL
    if s.startswith("HK."):
        return (TrdMarket.HK, s)
    if s.startswith("US."):
        return (TrdMarket.US, s)

    # Suffix format: 00700.HK, AAPL.US
    if s.endswith(".HK"):
        return (TrdMarket.HK, f"HK.{s[:-3]}")
    if s.endswith(".US"):
        return (TrdMarket.US, f"US.{s[:-3]}")

    # A-share — not supported by Futu
    if s.endswith(".SH") or s.endswith(".SZ"):
        raise ValueError(f"Futu does not support A-shares ({symbol}). Use HK or US markets.")

    # Bare ticker → try resolver first, then assume US
    try:
        from tradingagents.dataflows.stock_resolver import to_futu_trade
        futu_code, market = to_futu_trade(s)
        return (TrdMarket.HK if market == "HK" else TrdMarket.US, futu_code)
    except ImportError:
        pass
    return (TrdMarket.US, f"US.{s}")


def _symbol_from_futu_code(code: str) -> str:
    """Convert Futu code (e.g., 'HK.00700') back to our symbol format."""
    return code


# ── Trade context factory ────────────────────────────────────────────────────

def _need_encrypt() -> bool:
    """Check if RSA encryption is needed (remote host only, not localhost)."""
    host = _opend_host()
    if host in ("127.0.0.1", "localhost"):
        return False
    return _get_rsa_path() is not None


def _get_trade_ctx(symbol: Optional[str] = None):
    """Create an OpenSecTradeContext for simulated trading.

    If symbol is provided, uses its market; otherwise defaults to HK.
    Caller MUST close the context when done (use try/finally pattern).
    """
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm, SysConfig

    encrypt = _need_encrypt()
    if encrypt:
        rsa_path = _get_rsa_path()
        SysConfig.enable_proto_encrypt(is_encrypt=True)
        SysConfig.set_init_rsa_file(rsa_path)

    if symbol:
        market, _ = _to_futu_trade_code(symbol)
    else:
        market = TrdMarket.HK

    return OpenSecTradeContext(
        filter_trdmarket=market,
        host=_opend_host(),
        port=_opend_port(),
        security_firm=SecurityFirm.FUTUSECURITIES,
        is_encrypt=True if encrypt else None,
    )


# ── Core service methods ────────────────────────────────────────────────────

def get_account(trd_env: str = "SIMULATE", trd_market: str = "HK") -> AccountInfo:
    """Query simulated trading account financial information.

    Uses Futu accinfo_query to retrieve total assets, cash balance,
    frozen funds, and position market value.

    Args:
        trd_env: Trading environment. Only "SIMULATE" is supported.
        trd_market: "HK" or "US". Must match the market to see correct account.

    Returns:
        AccountInfo with all financial fields populated.

    Raises:
        RuntimeError: If FutuOpenD is offline or API call fails.
        ValueError: If trd_env is not "SIMULATE".
    """
    from futu import RET_OK, TrdEnv, TrdMarket, OpenSecTradeContext, SecurityFirm, SysConfig

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    market = TrdMarket.US if trd_market.upper() == "US" else TrdMarket.HK
    encrypt = _need_encrypt()
    if encrypt:
        rsa_path = _get_rsa_path()
        SysConfig.enable_proto_encrypt(is_encrypt=True)
        SysConfig.set_init_rsa_file(rsa_path)
    ctx = OpenSecTradeContext(
        host=_opend_host(),
        port=_opend_port(),
        filter_trdmarket=market,
        security_firm=SecurityFirm.FUTUSECURITIES,
        is_encrypt=encrypt,
    )
    try:
        ret, data = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE)
        if ret != RET_OK:
            raise RuntimeError(f"Futu accinfo_query failed: {data}")

        if data is None or data.empty:
            raise RuntimeError("No account data returned from FutuOpenD")

        row = data.iloc[0]
        currency_raw = str(row.get("currency", "N/A"))
        currency = currency_raw if currency_raw not in ("N/A", "", "None") else ("USD" if trd_market.upper() == "US" else "HKD")
        return AccountInfo(
            market=trd_market.upper(),
            total_assets=_safe_float(row.get("total_assets")),
            cash_balance=_safe_float(row.get("cash")),
            frozen_cash=_safe_float(row.get("frozen_cash")),
            market_val=_safe_float(row.get("market_val")),
            currency=currency,
            available_cash=_safe_float(row.get("poweravl", row.get("cash"))),
            unrealized_pnl=_safe_float(row.get("unrealized_pl")),
            realized_pnl=_safe_float(row.get("realized_pl")),
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
    trd_market: str = "HK",
    page_size: int = 100,
    page_index: int = 0,
) -> List[Position]:
    """Query simulated trading positions.

    Args:
        trd_env: Trading environment. Only "SIMULATE" is supported.
        trd_market: "HK" or "US". Determines which Futu account to query.
        page_size: Number of positions per page (max 1000).
        page_index: Page index (0-based).

    Returns:
        List of Position objects.

    Raises:
        RuntimeError: If FutuOpenD is offline or API call fails.
    """
    from futu import RET_OK, TrdEnv, TrdMarket, OpenSecTradeContext, SecurityFirm, SysConfig

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    market = TrdMarket.US if trd_market.upper() == "US" else TrdMarket.HK
    encrypt = _need_encrypt()
    if encrypt:
        rsa_path = _get_rsa_path()
        SysConfig.enable_proto_encrypt(is_encrypt=True)
        SysConfig.set_init_rsa_file(rsa_path)
    ctx = OpenSecTradeContext(
        host=_opend_host(),
        port=_opend_port(),
        filter_trdmarket=market,
        security_firm=SecurityFirm.FUTUSECURITIES,
        is_encrypt=True if encrypt else None,
    )
    try:
        ret, data = ctx.position_list_query(
            trd_env=TrdEnv.SIMULATE,
        )
        if ret != RET_OK:
            raise RuntimeError(f"Futu position_list_query failed: {data}")

        if data is None or data.empty:
            return []

        # Collect codes for snapshot lookup
        raw_rows = []
        codes_for_snapshot = []
        for _, row in data.iterrows():
            qty = int(row.get("qty", 0))
            if qty == 0:
                continue
            raw_rows.append(row)
            codes_for_snapshot.append(str(row.get("code", "")))

        # Fetch prev_close via quote snapshot
        prev_close_map: dict[str, float] = {}
        if codes_for_snapshot:
            try:
                from futu import OpenQuoteContext
                qctx = OpenQuoteContext(
                    host=_opend_host(), port=_opend_port(),
                    security_firm=SecurityFirm.FUTUSECURITIES,
                    is_encrypt=True if encrypt else None,
                )
                try:
                    ret2, snap = qctx.get_market_snapshot(codes_for_snapshot)
                    if ret2 == RET_OK and snap is not None and not snap.empty:
                        for _, sr in snap.iterrows():
                            prev_close_map[str(sr.get("code", ""))] = float(sr.get("prev_close_price", 0) or 0)
                finally:
                    qctx.close()
            except Exception:
                pass  # prev_close stays 0

        positions = []
        for row in raw_rows:
            qty = int(row.get("qty", 0))
            code = str(row.get("code", ""))
            cost_price = float(row.get("cost_price", 0))
            current_price = float(row.get("nominal_price", row.get("last_price", 0)))
            market_val = float(row.get("market_val", 0))
            unrealized_pnl = float(row.get("pl_val", 0))
            pl_ratio = float(row.get("pl_ratio", 0))
            prev_close = prev_close_map.get(code, 0.0)

            positions.append(Position(
                code=code,
                symbol=_symbol_from_futu_code(code),
                stock_name=str(row.get("stock_name", "")),
                qty=qty,
                cost_price=cost_price,
                current_price=current_price,
                prev_close=prev_close,
                market_val=market_val,
                cost_val=round(cost_price * qty, 2),
                unrealized_pnl=unrealized_pnl,
                unrealized_pnl_pct=round(pl_ratio, 2) if pl_ratio else 0.0,
                today_pnl=round((current_price - prev_close) * qty, 2) if prev_close else 0.0,
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
    from futu import RET_OK, TrdEnv, TrdSide, TrdMarket, ModifyOrderOp, SecurityFirm
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
    ctx = _get_trade_ctx(symbol)
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
        order_id = str(row.get("order_id", ""))
        order_status = str(row.get("order_status", "SUBMITTED"))

        # ── Record deal locally (Futu sim doesn't support deal_list_query) ──
        # Query actual fill price — poll up to 3 times (sim orders fill in 2-5s)
        import time
        deal_price = float(row.get("price", price))
        deal_qty = float(row.get("qty", quantity))
        deal_stock_name = str(row.get("stock_name", ""))

        if order_status in ("FILLED_ALL", "FILLED_PART"):
            deal_price = float(row.get("dealt_avg_price", row.get("price", price)))
            deal_qty = float(row.get("dealt_qty", row.get("qty", quantity)))
        else:
            for _attempt in range(3):
                time.sleep(2)
                try:
                    ret2, data2 = ctx.order_list_query(trd_env=TrdEnv.SIMULATE)
                    if ret2 == RET_OK and data2 is not None and not data2.empty:
                        for _, orow in data2.iterrows():
                            if str(orow.get("order_id", "")) == order_id:
                                st = str(orow.get("order_status", ""))
                                if not deal_stock_name:
                                    deal_stock_name = str(orow.get("stock_name", ""))
                                if st in ("FILLED_ALL", "FILLED_PART"):
                                    deal_price = float(orow.get("dealt_avg_price", price))
                                    deal_qty = float(orow.get("dealt_qty", quantity))
                                    break
                        else:
                            continue
                        break  # inner break → exit retry loop
                except Exception:
                    pass

        deal_market = "HK" if market == TrdMarket.HK else "US"
        _record_deal(
            order_id=order_id,
            code=code,
            stock_name=deal_stock_name,
            trd_side=side_upper,
            deal_market=deal_market,
            qty=deal_qty,
            price=deal_price,
            order_type=_order_type_label(order_type_upper),
            currency="HKD" if deal_market == "HK" else "USD",
        )

        return OrderResult(
            order_id=order_id,
            code=str(row.get("code", code)),
            side=side_upper,
            price=deal_price,
            qty=deal_qty,
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
    ctx = _get_trade_ctx(symbol)
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
    trd_market: str = "HK",
    page_size: int = 100,
    page_index: int = 0,
) -> List[OrderInfo]:
    """Query simulated trading orders.

    Args:
        symbol: Optional stock symbol filter. If None, queries the specified market.
        trd_env: Trading environment. Only "SIMULATE" is supported.
        trd_market: "HK" or "US". Determines which market context to query.
        page_size: Number of orders per page (max 1000).
        page_index: Page index (0-based).

    Returns:
        List of OrderInfo objects.
    """
    from futu import RET_OK, TrdEnv, OpenSecTradeContext, TrdMarket, SecurityFirm, SysConfig

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    if symbol:
        market_val, code = _to_futu_trade_code(symbol)
        ctx = _get_trade_ctx(symbol)
        code_filter = code
    else:
        market_val = TrdMarket.US if trd_market.upper() == "US" else TrdMarket.HK
        encrypt = _need_encrypt()
        if encrypt:
            rsa_path = _get_rsa_path()
            SysConfig.enable_proto_encrypt(is_encrypt=True)
            SysConfig.set_init_rsa_file(rsa_path)
        ctx = OpenSecTradeContext(
            host=_opend_host(),
            port=_opend_port(),
            filter_trdmarket=market_val,
            security_firm=SecurityFirm.FUTUSECURITIES,
            is_encrypt=True if encrypt else None,
        )
        code_filter = None

    try:
        ret, data = ctx.order_list_query(
            trd_env=TrdEnv.SIMULATE,
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
    """Query executed deals (filled trades).

    For SIMULATE: reads from local DB (Futu sim doesn't support deal_list_query).
    For REAL: queries Futu deal_list_query directly.

    Args:
        symbol: Optional stock symbol filter.
        trd_env: "SIMULATE" or "REAL".
        page_size: Number of deals per page (max 1000).
        page_index: Page index (0-based).

    Returns:
        List of DealInfo objects.
    """
    if trd_env == "SIMULATE":
        return _get_local_deals(symbol, page_size, page_index)

    # Real trading: query Futu directly
    from futu import RET_OK, TrdEnv, OpenSecTradeContext, SecurityFirm

    ctx = _get_trade_ctx(symbol) if symbol else _get_trade_ctx()
    try:
        ret, data = ctx.deal_list_query(trd_env=TrdEnv.REAL)
        if ret != RET_OK:
            raise RuntimeError(f"Futu deal_list_query failed: {data}")

        if data is None or data.empty:
            return []

        deals = []
        for _, row in data.iterrows():
            deals.append(DealInfo(
                deal_id=str(row.get("deal_id", "")),
                code=str(row.get("code", "")),
                stock_name=str(row.get("stock_name", "")),
                side=str(row.get("trd_side", "")),
                deal_market=str(row.get("deal_market", "")),
                order_type=str(row.get("order_type", "NORMAL")),
                qty=float(row.get("qty", 0)),
                price=float(row.get("price", 0)),
                create_time=str(row.get("create_time", "")),
                status=str(row.get("status", "FILLED")),
                currency=str(row.get("currency", "HKD")),
            ))
        return deals
    finally:
        ctx.close()


def _get_local_deals(
    symbol: Optional[str] = None,
    page_size: int = 100,
    page_index: int = 0,
) -> List[DealInfo]:
    """Read simulated deals from local DB."""
    from api.database import SessionLocal, SimDealDB

    db = SessionLocal()
    try:
        query = db.query(SimDealDB).order_by(SimDealDB.create_time.desc())
        if symbol:
            # Normalize symbol for matching
            query = query.filter(SimDealDB.code == symbol)

        offset = page_index * page_size
        rows = query.offset(offset).limit(page_size).all()

        return [
            DealInfo(
                deal_id=r.deal_id,
                code=r.code,
                stock_name=r.stock_name or "",
                side=r.trd_side,
                deal_market=r.deal_market or "",
                order_type=r.order_type or "NORMAL",
                qty=r.qty,
                price=r.price,
                create_time=r.create_time,
                status=r.status or "FILLED",
                currency=r.currency or "HKD",
            )
            for r in rows
        ]
    finally:
        db.close()




# ── Additional trading queries ──────────────────────────────────────────────


def get_acc_list(trd_market: str = "HK") -> List[Dict[str, Any]]:
    """Get list of trading accounts (simulated + real).

    Args:
        trd_market: "HK" or "US". Must match the market to see correct accounts.

    Returns:
        List of account dicts with acc_id, sim_acc_type, trd_env, etc.
    """
    from futu import TrdMarket, OpenSecTradeContext, SecurityFirm, SysConfig

    market = TrdMarket.US if trd_market.upper() == "US" else TrdMarket.HK
    encrypt = _need_encrypt()
    if encrypt:
        rsa_path = _get_rsa_path()
        SysConfig.enable_proto_encrypt(is_encrypt=True)
        SysConfig.set_init_rsa_file(rsa_path)
    ctx = OpenSecTradeContext(
        filter_trdmarket=market,
        host=_opend_host(),
        port=_opend_port(),
        security_firm=SecurityFirm.FUTUSECURITIES,
        is_encrypt=True if encrypt else None,
    )
    try:
        ret, data = ctx.get_acc_list()
        if ret != 0:
            raise RuntimeError(f"Futu get_acc_list failed: {data}")
        if data is None or data.empty:
            return []
        return data.to_dict("records")
    finally:
        ctx.close()




def get_trading_info(
    symbol: str,
    price: float,
    order_type: str = "NORMAL",
    trd_env: str = "SIMULATE",
    acc_id: int = 0,
) -> Dict[str, Any]:
    """Query max buy/sell quantity and other trading info before placing an order.

    Args:
        symbol: Stock symbol (e.g. "00700.HK", "AAPL.US").
        price: Order price.
        order_type: "NORMAL", "MARKET", "AUCTION_LIMIT".
        trd_env: Only "SIMULATE" supported.
        acc_id: Optional account ID. If 0, auto-detects simulated STOCK account.

    Returns:
        Dict with max_cash_buy, max_stock_sell, etc.
    """
    from futu import RET_OK, TrdEnv

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    market, code = _to_futu_trade_code(symbol)
    futu_order_type = _resolve_order_type(order_type)
    ctx = _get_trade_ctx(symbol)
    try:
        kwargs = dict(
            order_type=futu_order_type,
            code=code,
            price=price,
            trd_env=TrdEnv.SIMULATE,
        )
        if acc_id > 0:
            kwargs["acc_id"] = acc_id
        ret, data = ctx.acctradinginfo_query(**kwargs)
        if ret != RET_OK:
            raise RuntimeError(f"Futu acctradinginfo_query failed: {data}")
        if data is None or data.empty:
            return {}
        return data.iloc[0].to_dict()
    finally:
        ctx.close()


def get_history_orders(
    symbol: Optional[str] = None,
    status_filter: Optional[List[str]] = None,
    start: str = "",
    end: str = "",
    trd_env: str = "SIMULATE",
) -> List[Dict[str, Any]]:
    """Query historical orders (not limited to today).

    Args:
        symbol: Filter by symbol. None = all.
        status_filter: List of order statuses (e.g. ["FILLED_ALL", "CANCELLED_ALL"]).
        start: Start time "YYYY-MM-DD HH:MM:SS".
        end: End time "YYYY-MM-DD HH:MM:SS".
        trd_env: Only "SIMULATE" supported.

    Returns:
        List of order dicts.
    """
    from futu import RET_OK, TrdEnv, OrderStatus

    if trd_env != "SIMULATE":
        raise ValueError("SimTradingService only supports trd_env='SIMULATE'")

    ctx = _get_trade_ctx(symbol)
    try:
        futu_status = []
        if status_filter:
            status_map = {
                "SUBMITTED": OrderStatus.SUBMITTED,
                "SUBMITTING": OrderStatus.SUBMITTING,
                "FILLED_ALL": OrderStatus.FILLED_ALL,
                "FILLED_PART": OrderStatus.FILLED_PART,
                "CANCELLED_ALL": OrderStatus.CANCELLED_ALL,
                "CANCELLED_PART": OrderStatus.CANCELLED_PART,
                "FAILED": OrderStatus.FAILED,
                "DISABLED": OrderStatus.DISABLED,
                "DEAD": OrderStatus.DEAD,
            }
            futu_status = [status_map[s] for s in status_filter if s in status_map]

        # Convert symbol to Futu code format if provided
        futu_code = ""
        if symbol:
            try:
                from tradingagents.dataflows.providers.futu_provider import FutuProvider
                _, futu_code = FutuProvider._to_futu_code(None, symbol)
            except Exception:
                futu_code = symbol

        ret, data = ctx.history_order_list_query(
            status_filter_list=futu_status,
            code=futu_code,
            start=start,
            end=end,
            trd_env=TrdEnv.SIMULATE,
        )
        if ret != RET_OK:
            raise RuntimeError(f"Futu history_order_list_query failed: {data}")
        if data is None or data.empty:
            return []
        return data.to_dict("records")
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
        "AUCTION": OrderType.AUCTION,
    }
    return mapping.get(order_type_str, OrderType.NORMAL)


# Futu OrderType → display label (industry standard)
_ORDER_TYPE_LABEL = {
    "NORMAL": "LMT",
    "MARKET": "MKT",
    "AUCTION_LIMIT": "AUCTION_LMT",
    "AUCTION": "AUCTION_MKT",
    "STOP": "STOP",
    "STOP_LIMIT": "STOP_LMT",
    "TRAILING_STOP": "TRAIL_STOP",
}


def _order_type_label(futu_type: str) -> str:
    """Convert Futu order type string to display label."""
    return _ORDER_TYPE_LABEL.get(futu_type, futu_type)


# ── Serialization helpers ────────────────────────────────────────────────────

def account_to_dict(info: AccountInfo) -> Dict[str, Any]:
    """Serialize AccountInfo to dict for API response."""
    return {
        "market": info.market,
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
        "stock_name": p.stock_name,
        "qty": p.qty,
        "cost_price": p.cost_price,
        "current_price": p.current_price,
        "prev_close": p.prev_close,
        "market_val": p.market_val,
        "cost_val": p.cost_val,
        "unrealized_pnl": p.unrealized_pnl,
        "unrealized_pnl_pct": p.unrealized_pnl_pct,
        "today_pnl": p.today_pnl,
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
        "deal_id": d.deal_id,
        "code": d.code,
        "stock_name": d.stock_name,
        "side": d.side,
        "deal_market": d.deal_market,
        "order_type": d.order_type,
        "qty": d.qty,
        "price": d.price,
        "create_time": d.create_time,
        "status": d.status,
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
