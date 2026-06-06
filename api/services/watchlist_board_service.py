"""Watchlist Board Service — watchlist items with live Futu quotes + analysis."""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from typing import Any

from sqlalchemy.orm import Session

from api.database import ReportDB, WatchlistItemDB
from api.services.quote_ws_manager import quote_ws_manager
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.market_calendar import today_str, previous_trading_day

REFRESH_INTERVAL_SECONDS = 20
logger = logging.getLogger(__name__)


def get_watchlist_board(db: Session, user_id: str) -> dict[str, Any]:
    previous_trade_date = previous_trading_day(today_str("HK"), "HK")

    # 1. Get watchlist items from DB
    rows = (
        db.query(WatchlistItemDB)
        .filter(WatchlistItemDB.user_id == user_id)
        .order_by(WatchlistItemDB.sort_order, WatchlistItemDB.created_at)
        .all()
    )
    symbols = [row.symbol for row in rows]

    # 2. Convert symbols to Futu-compatible format for quotes
    from tradingagents.dataflows.stock_resolver import to_futu, to_display
    futu_symbols = []
    symbol_map = {}  # futu_code -> canonical
    for sym in symbols:
        futu_code = to_futu(sym)
        # HK.00700 -> 00700.HK format for FutuProvider
        if futu_code.startswith("HK."):
            display_code = futu_code[3:] + ".HK"
        elif futu_code.startswith("US."):
            bare = futu_code[3:]
            # Numeric-only codes with US prefix are likely HK (stock_resolver fallback)
            if bare.isdigit():
                display_code = bare.zfill(5) + ".HK"
            else:
                display_code = bare
        else:
            display_code = sym
        futu_symbols.append(display_code)
        symbol_map[display_code] = sym

    # 3. Use WebSocket cached quotes if available, otherwise fetch directly
    cached_quotes = quote_ws_manager.latest_quotes
    cached_states = quote_ws_manager.latest_states
    if cached_quotes and any(s in cached_quotes for s in symbols):
        quotes = {s: cached_quotes[s] for s in symbols if s in cached_quotes}
        market_states = {s: cached_states.get(s, "") for s in symbols}
    else:
        raw_quotes = _fetch_live_quotes(futu_symbols)
        quotes = {}
        for futu_code, q in raw_quotes.items():
            canonical = symbol_map.get(futu_code, futu_code)
            quotes[canonical] = q
        from tradingagents.dataflows.providers.futu_provider import get_market_state
        market_states = get_market_state(symbols)

    # 4. Fetch analysis reports
    reports = _select_reports_for_symbols(db, user_id, symbols, previous_trade_date)

    items: list[dict[str, Any]] = []
    for row in rows:
        symbol = row.symbol
        quote = quotes.get(symbol, {})
        live_price = _to_float(quote.get("price"))

        items.append(
            {
                "symbol": symbol,
                "name": to_display(symbol) if symbol else symbol,
                "live_price": live_price,
                "day_open": _to_float(quote.get("open")),
                "price_change": _to_float(quote.get("change")),
                "price_change_pct": _to_float(quote.get("change_pct")),
                "day_high": _to_float(quote.get("high")),
                "day_low": _to_float(quote.get("low")),
                "volume": _to_float(quote.get("volume")),
                "amount": _to_float(quote.get("amount")),
                "prev_close": _to_float(quote.get("prev_close")),
                "amplitude": _to_float(quote.get("amplitude")),
                "turnover_rate": _to_float(quote.get("turnover_rate")),
                "turnover": _to_float(quote.get("turnover")),
                "market_state": market_states.get(symbol, ""),
                "quote_time": quote.get("quote_time"),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "analysis": _serialize_report_summary(reports.get(symbol), previous_trade_date),
            }
        )

    return {
        "previous_trade_date": previous_trade_date,
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
        "subscription_used": quote_ws_manager.subscription_count,
        "subscription_limit": 300,
        "items": items,
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
                    "turnover": row.get("turnover"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "open": row.get("open"),
                    "prev_close": row.get("prev_close"),
                    "amplitude": row.get("amplitude"),
                    "turnover_rate": row.get("turnover_rate"),
                }
        return quotes
    except Exception as exc:
        logger.warning("[watchlist-board] realtime quote fetch failed: %s", exc)
        return {}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except Exception:
        return None
