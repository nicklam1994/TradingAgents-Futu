"""Market calendar for US and HK trading days.

Uses Futu OpenD's ``request_trading_days`` API as the primary source,
with yfinance (US) as fallback when Futu is unavailable.

Replaces the deprecated ``trade_calendar.py`` (A-share focused).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

HK_TZ = ZoneInfo("Asia/Hong_Kong")
US_TZ = ZoneInfo("America/New_York")


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_hk() -> datetime:
    return datetime.now(HK_TZ)


def now_us() -> datetime:
    return datetime.now(US_TZ)


def today_str(tz: str = "HK") -> str:
    """Return today's date string in the given timezone."""
    dt = now_hk() if tz == "HK" else now_us()
    return dt.strftime("%Y-%m-%d")


def _parse(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


# ── Futu-backed calendar ─────────────────────────────────────────────────────

def _futu_trading_days(
    market: str, start: str, end: str
) -> tuple[list[date], set[date]]:
    """Fetch trading days from Futu OpenD. Returns (sorted_list, set).

    *market* is ``"HK"`` or ``"US"``.
    Falls back to empty if Futu is unreachable.
    """
    try:
        import futu
        from futu import TradeDateMarket

        mkt_enum = TradeDateMarket.HK if market == "HK" else TradeDateMarket.US
        ctx = futu.OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            ret, data = ctx.request_trading_days(
                market=mkt_enum, start=start, end=end
            )
            if ret != futu.RET_OK or not isinstance(data, list):
                logger.warning("[MarketCal] Futu request_trading_days failed: %s", data)
                return [], set()
            dates = sorted(
                _parse(str(row["time"]))
                for row in data
                if isinstance(row, dict) and row.get("trade_date_type") == "WHOLE"
            )
            return dates, set(dates)
        finally:
            ctx.close()
    except Exception as e:
        logger.warning("[MarketCal] Futu unavailable (%s), using fallback", e)
        return [], set()


def _yfinance_us_trading_days(
    start: str, end: str
) -> tuple[list[date], set[date]]:
    """Fallback: use yfinance's US market calendar."""
    try:
        import pandas as pd
        import yfinance as yf

        spy = yf.Ticker("SPY")
        hist = spy.history(start=start, end=end)
        if hist.empty:
            return [], set()
        dates = sorted(
            d.date() for d in pd.to_datetime(hist.index)
        )
        return dates, set(dates)
    except Exception as e:
        logger.warning("[MarketCal] yfinance fallback failed: %s", e)
        return [], set()


def _load_trading_days(
    market: str, start: str, end: str
) -> tuple[list[date], set[date]]:
    """Load trading days: Futu first, then fallback."""
    dates, dates_set = _futu_trading_days(market, start, end)
    if dates:
        return dates, dates_set
    if market == "US":
        return _yfinance_us_trading_days(start, end)
    return [], set()


# ── Public API ────────────────────────────────────────────────────────────────

def is_trading_day(date_str: str, market: str = "HK") -> bool:
    """Check if *date_str* is a trading day for the given market.

    Falls back to weekday-only check if calendar data is unavailable.
    """
    d = _parse(date_str)
    year_start = f"{d.year}-01-01"
    year_end = f"{d.year + 1}-01-01"
    _, dates_set = _load_trading_days(market, year_start, year_end)
    if dates_set:
        return d in dates_set
    return d.weekday() < 5


def previous_trading_day(date_str: str, market: str = "HK") -> str:
    """Return the most recent trading day on or before *date_str*."""
    d = _parse(date_str)
    year_start = f"{d.year - 1}-01-01"
    year_end = f"{d.year + 1}-01-01"
    dates, _ = _load_trading_days(market, year_start, year_end)
    if dates:
        for dt in reversed(dates):
            if dt <= d:
                return dt.strftime("%Y-%m-%d")
    # Fallback: walk backwards skipping weekends
    cur = d
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur.strftime("%Y-%m-%d")


def next_trading_day(date_str: str, market: str = "HK") -> str:
    """Return the next trading day after *date_str*."""
    d = _parse(date_str)
    year_start = f"{d.year}-01-01"
    year_end = f"{d.year + 1}-01-01"
    dates, _ = _load_trading_days(market, year_start, year_end)
    if dates:
        for dt in dates:
            if dt > d:
                return dt.strftime("%Y-%m-%d")
    cur = d + timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur.strftime("%Y-%m-%d")


def market_phase(now: datetime | None = None, market: str = "HK") -> str:
    """Return the current market session phase.

    Returns: ``"pre_open"`` / ``"in_session"`` / ``"lunch_break"`` /
    ``"post_close"`` / ``"closed"``.

    HK: 09:30-12:00, 13:00-16:00 HKT
    US: 09:30-16:00 ET (no lunch break)
    """
    if market == "HK":
        now_dt = now or now_hk()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=HK_TZ)
        else:
            now_dt = now_dt.astimezone(HK_TZ)
        today = now_dt.strftime("%Y-%m-%d")
        if not is_trading_day(today, "HK"):
            return "closed"
        t = now_dt.time()
        if t < time(9, 30):
            return "pre_open"
        if time(9, 30) <= t < time(12, 0):
            return "in_session"
        if time(12, 0) <= t < time(13, 0):
            return "lunch_break"
        if time(13, 0) <= t < time(16, 0):
            return "in_session"
        return "post_close"
    else:
        now_dt = now or now_us()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=US_TZ)
        else:
            now_dt = now_dt.astimezone(US_TZ)
        today = now_dt.strftime("%Y-%m-%d")
        if not is_trading_day(today, "US"):
            return "closed"
        t = now_dt.time()
        if t < time(9, 30):
            return "pre_open"
        if time(9, 30) <= t < time(16, 0):
            return "in_session"
        return "post_close"


def no_data_reason(date_str: str, market: str = "HK") -> str:
    """Return a human-readable reason why data might be missing."""
    if not is_trading_day(date_str, market):
        return f"N/A：非交易日（{market} 休市）"

    tz_str = "HK" if market == "HK" else "US"
    today = today_str(tz_str)
    if date_str == today:
        phase = market_phase(market=market)
        if phase == "pre_open":
            return f"N/A：今日尚未开盘（{market}）"
        if phase in ("in_session", "lunch_break"):
            return f"N/A：今日盘中，日线未收盘（{market}，可参考实时价）"
        if phase == "post_close":
            return f"N/A：今日已收盘，数据源尚未更新（{market}）"

    return f"N/A：该交易日暂无数据（{market}，可能停牌或数据延迟）"
