from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from typing import Any

from sqlalchemy.orm import Session

from api.database import ReportDB
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.market_calendar import today_str, previous_trading_day


REFRESH_INTERVAL_SECONDS = 20
logger = logging.getLogger(__name__)

# ── Position Cache ────────────────────────────────────────────────────────────
# Cache Futu positions so the board works even when OpenD is temporarily down.
# _position_cache stores the last successful fetch result.
# _position_cache_ts stores the timestamp of the last successful fetch.
# _POSITION_CACHE_TTL seconds before we consider the cache stale (but still usable).
_POSITION_CACHE_TTL = 300  # 5 minutes
_position_cache: list[dict[str, Any]] = []
_position_cache_ts: float = 0.0


def _fetch_futu_positions() -> list[dict[str, Any]]:
    """Fetch real positions from FutuOpenD. Returns cached data on failure."""
    global _position_cache, _position_cache_ts

    try:
        from tradingagents.dataflows.providers.futu_provider import FutuProvider
        provider = FutuProvider()
        positions = provider.get_positions()
        if positions:
            _position_cache = positions
            _position_cache_ts = time.time()
            logger.info("[tracking-board] fetched %d positions from FutuOpenD", len(positions))
            return positions
        else:
            # OpenD returned empty — might be disconnected, use cache
            logger.info("[tracking-board] FutuOpenD returned empty, using cache (%d items)", len(_position_cache))
            return _position_cache
    except Exception as exc:
        logger.warning("[tracking-board] FutuOpenD fetch failed: %s, using cache (%d items)", exc, len(_position_cache))
        return _position_cache


def _is_cache_fresh() -> bool:
    """Check if cached positions are still fresh."""
    return _position_cache and (time.time() - _position_cache_ts) < _POSITION_CACHE_TTL


def get_tracking_board(db: Session, user_id: str) -> dict[str, Any]:
    previous_trade_date = previous_trading_day(today_str("HK"), "HK")

    # Fetch positions from Futu (with cache fallback)
    positions = _fetch_futu_positions()
    
    # Calculate stats from positions
    total_realized_pnl = sum(_to_float(p.get("realized_pl")) or 0 for p in positions)
    total_unrealized_pnl = sum(_to_float(p.get("unrealized_pl")) or 0 for p in positions)
    profitable_count = sum(1 for p in positions if (_to_float(p.get("unrealized_pl")) or 0) > 0)
    total_positions = len(positions)
    win_rate = round(profitable_count / total_positions * 100, 1) if total_positions > 0 else None
    symbols = [p["symbol"] for p in positions]

    # Fetch live quotes for all symbols
    quotes = _fetch_live_quotes(symbols)
    
    # Fetch market states
    from tradingagents.dataflows.providers.futu_provider import get_market_state
    market_states = get_market_state(symbols) if symbols else {}

    # Fetch analysis reports
    reports = _select_reports_for_symbols(db, user_id, symbols, previous_trade_date)

    # Calculate live market values first for position ratio
    live_values: dict[str, float] = {}
    for pos in positions:
        symbol = pos["symbol"]
        quote = quotes.get(symbol, {})
        live_price = _to_float(quote.get("price")) or _to_float(pos.get("nominal_price"))
        qty = _to_float(pos.get("qty"))
        market_val = _to_float(pos.get("market_val"))
        if live_price and qty:
            live_values[symbol] = round(live_price * qty, 2)
        else:
            live_values[symbol] = market_val or 0
    total_market_value = sum(live_values.values()) or 1

    items: list[dict[str, Any]] = []
    for pos in positions:
        symbol = pos["symbol"]
        quote = quotes.get(symbol, {})
        live_price = _to_float(quote.get("price")) or _to_float(pos.get("nominal_price"))
        qty = _to_float(pos.get("qty"))
        cost_price = _to_float(pos.get("cost_price"))
        market_val = _to_float(pos.get("market_val"))
        pl_ratio = _to_float(pos.get("pl_ratio"))
        pl_val = _to_float(pos.get("pl_val"))

        # Recalculate with live price if available
        if live_price and qty and cost_price:
            live_market_value = round(live_price * qty, 2)
            floating_pnl = round((live_price - cost_price) * qty, 2)
            floating_pnl_pct = round(((live_price - cost_price) / cost_price) * 100, 2) if cost_price else None
        else:
            live_market_value = market_val
            floating_pnl = pl_val
            floating_pnl_pct = pl_ratio

        items.append(
            {
                "symbol": symbol,
                "name": pos.get("stock_name", symbol),
                "current_position": qty,
                "available_position": _to_float(pos.get("can_sell_qty")),
                "average_cost": cost_price,
                "market_value": market_val,
                "live_market_value": live_market_value,
                "floating_pnl": floating_pnl,
                "floating_pnl_pct": floating_pnl_pct,
                "live_price": live_price,
                "day_open": _to_float(quote.get("open")),
                "price_change": _to_float(quote.get("change")),
                "price_change_pct": _to_float(quote.get("change_pct")),
                "day_high": _to_float(quote.get("high")),
                "day_low": _to_float(quote.get("low")),
                "previous_close": _to_float(quote.get("previous_close")),
                "volume": _to_float(quote.get("volume")),
                "amount": _to_float(quote.get("amount")),
                "quote_time": quote.get("quote_time"),
                "quote_source": quote.get("source"),
                "currency": pos.get("currency", ""),
                "position_side": pos.get("position_side", "LONG"),
                "market_state": market_states.get(symbol, ""),
                "lot_size": int(quote.get("lot_size") or 0) or (100 if symbol.endswith(".HK") else 1),
                "current_position_pct": round(live_values.get(symbol, 0) / total_market_value * 100, 1),
                "analysis": _serialize_report_summary(reports.get(symbol), previous_trade_date),
            }
        )

    return {
        "previous_trade_date": previous_trade_date,
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
        "cache_fresh": _is_cache_fresh(),
        "last_sync_ts": _position_cache_ts,
        "items": items,
        "stats": {
            "cumulative_profit": total_realized_pnl if total_realized_pnl > 0 else 0,
            "cumulative_loss": total_realized_pnl if total_realized_pnl < 0 else 0,
            "win_rate": win_rate,
        },
    }


def _select_reports_for_symbols(
    db: Session,
    user_id: str,
    symbols: list[str],
    previous_trade_date: str,
) -> dict[str, ReportDB]:
    if not symbols:
        return {}

    rows = (
        db.query(ReportDB)
        .filter(
            ReportDB.user_id == user_id,
            ReportDB.symbol.in_(symbols),
            ReportDB.status == "completed",
        )
        .order_by(ReportDB.trade_date.desc(), ReportDB.created_at.desc())
        .all()
    )

    exact_previous: dict[str, ReportDB] = {}
    latest_before_previous: dict[str, ReportDB] = {}
    latest_any: dict[str, ReportDB] = {}

    for row in rows:
        if row.symbol not in latest_any:
            latest_any[row.symbol] = row
        if row.trade_date == previous_trade_date and row.symbol not in exact_previous:
            exact_previous[row.symbol] = row
        if row.trade_date <= previous_trade_date and row.symbol not in latest_before_previous:
            latest_before_previous[row.symbol] = row

    selected: dict[str, ReportDB] = {}
    for symbol in symbols:
        report = exact_previous.get(symbol) or latest_before_previous.get(symbol) or latest_any.get(symbol)
        if report:
            selected[symbol] = report
    return selected


def _serialize_report_summary(report: ReportDB | None, previous_trade_date: str) -> dict[str, Any] | None:
    if report is None:
        return None

    return {
        "report_id": report.id,
        "trade_date": report.trade_date,
        "is_previous_trade_day": report.trade_date == previous_trade_date,
        "decision": report.decision,
        "direction": report.direction,
        "high_price": _to_float(report.target_price),
        "low_price": _to_float(report.stop_loss_price),
        "trader_advice_summary": _summarize_trader_advice(
            report.trader_investment_plan,
            fallback_text=report.final_trade_decision,
        ),
        "trader_investment_plan": report.trader_investment_plan,
        "final_trade_decision": report.final_trade_decision,
    }


def _summarize_trader_advice(text: str | None, fallback_text: str | None = None) -> str | None:
    for source in (text, fallback_text):
        if not source:
            continue

        for pattern in (
            r"最终交易建议[:：]\s*([^\n]+)",
            r"结论[:：]\s*([^\n]+)",
            r"建议动作[:：]\s*([^\n]+)",
            r"方向[:：]\s*([^\n]+)",
        ):
            match = re.search(pattern, source, re.IGNORECASE)
            if match:
                return _clip_summary(match.group(1))

        lines = [
            _clip_summary(line.strip(" -*\t"))
            for line in _strip_markdown(source).splitlines()
            if line.strip()
        ]
        for line in lines:
            if len(line) >= 6 and not re.match(r"^[一二三四五六七八九十0-9]+[、.)：:]?$", line):
                return line
    return None


def _strip_markdown(text: str) -> str:
    cleaned = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    cleaned = cleaned.replace("\r", "\n")
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*|__", "", cleaned)
    cleaned = re.sub(r"^\s*#+\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cleaned)
    return cleaned


def _clip_summary(text: str | None) -> str | None:
    if text is None:
        return None
    compact = re.sub(r"\s+", " ", text).strip(" ，,;；。")
    if not compact:
        return None
    return compact[:96]


def _fetch_live_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    try:
        result = route_to_vendor("get_realtime_quotes", symbols)
        if not result:
            return {}
        # FutuProvider returns CSV, parse it
        reader = csv.DictReader(io.StringIO(result))
        quotes = {}
        for row in reader:
            sym = row.get("symbol", "")
            if sym:
                quotes[sym] = {
                    "price": row.get("price"),
                    "change": row.get("change"),
                    "change_pct": row.get("change_pct"),
                    "volume": row.get("volume"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "open": row.get("open"),
                    "previous_close": row.get("prev_close"),
                    "lot_size": row.get("lot_size"),
                    "sec_status": row.get("sec_status"),
                }
        return quotes
    except Exception as exc:
        logger.warning("[tracking-board] realtime quote fetch failed: %s", exc)
        return {}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except Exception:
        return None
