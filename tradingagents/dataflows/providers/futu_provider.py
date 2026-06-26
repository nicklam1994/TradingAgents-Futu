"""Futu Provider — US/HK market data via FutuOpenD API.

Requires a running FutuOpenD instance (default: 127.0.0.1:11111).
Configure via FUTU_OPEND_HOST / FUTU_OPEND_PORT environment variables.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, date as date_type

import pandas as pd
from stockstats import wrap

from .base import BaseMarketDataProvider

logger = logging.getLogger(__name__)


# ── Request Cache for get_panel_data() ──

class _PanelCache:
    """In-memory cache for get_panel_data() with calendar-day TTL.

    Cache key: (frozenset(symbols), start_date, end_date, autype)
    Cache validity: same calendar day (does not expire on weekends/holidays)
    This avoids redundant API calls when multiple strategies request the same data.
    """

    def __init__(self, maxsize: int = 128):
        self._cache: Dict[tuple, tuple] = {}  # key -> (panel_dict, cache_date)
        self._maxsize = maxsize

    def _get_cache_date(self) -> date_type:
        """Get current date in trading timezone (simplified: use local date)."""
        return date_type.today()

    def _is_valid(self, cache_date: date_type) -> bool:
        """Check if cache is still valid (same trading day)."""
        return cache_date == self._get_cache_date()

    def get(self, symbols: tuple, start_date: str, end_date: str,
            autype: Optional[str]) -> Optional[Dict[str, pd.DataFrame]]:
        """Retrieve cached panel data if valid."""
        key = (frozenset(symbols), start_date, end_date, autype)
        if key in self._cache:
            panel, cache_date = self._cache[key]
            if self._is_valid(cache_date):
                logger.debug("Panel cache hit for %s", symbols)
                return panel
            else:
                # Expired — remove stale entry
                del self._cache[key]
                logger.debug("Panel cache expired for %s (was %s)", symbols, cache_date)
        return None

    def put(self, symbols: tuple, start_date: str, end_date: str,
            autype: Optional[str], panel: Dict[str, pd.DataFrame]) -> None:
        """Store panel data in cache."""
        key = (frozenset(symbols), start_date, end_date, autype)
        # Evict oldest entry if at capacity (FIFO)
        if len(self._cache) >= self._maxsize and key not in self._cache:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
            logger.debug("Panel cache evicted oldest entry (at maxsize=%d)", self._maxsize)
        # Deep copy DataFrames to prevent cache pollution from external mutations
        self._cache[key] = ({k: v.copy() for k, v in panel.items()}, self._get_cache_date())
        logger.debug("Panel cache stored for %s", symbols)

    def clear(self) -> None:
        """Clear all cached data."""
        self._cache.clear()


# Module-level cache instance
_panel_cache = _PanelCache()


def _opend_host() -> str:
    """Read FutuOpenD host from .env or environment variable."""
    # 1. Check environment variable first
    host = os.getenv("FUTU_OPEND_HOST")
    if host:
        return host
    # 2. Read from .env file
    try:
        from dotenv import dotenv_values
        env_vals = dotenv_values(".env")
        host = env_vals.get("FUTU_OPEND_HOST")
        if host:
            return host
    except Exception:
        pass
    # 3. Default
    return "127.0.0.1"


def _opend_port() -> int:
    """Read FutuOpenD port from .env or environment variable."""
    # 1. Check environment variable first
    port = os.getenv("FUTU_OPEND_PORT")
    if port:
        return int(port)
    # 2. Read from .env file
    try:
        from dotenv import dotenv_values
        env_vals = dotenv_values(".env")
        port = env_vals.get("FUTU_OPEND_PORT")
        if port:
            return int(port)
    except Exception:
        pass
    # 3. Default
    return 11111


class FutuProvider(BaseMarketDataProvider):
    """Market data provider backed by Futu OpenD API.

    Supports US and HK equities. A-share (SH/SZ) symbols are not supported
    by Futu and will raise NotImplementedError.
    """

    _encrypt_done = False  # Class-level flag for one-time RSA setup

    # ── Futu supported indicators ──
    INDICATOR_DESCRIPTIONS = {
        "close_50_sma": "50 日均线（SMA）：中期趋势指标。",
        "close_200_sma": "200 日均线（SMA）：长期趋势基准。",
        "close_10_ema": "10 日指数均线（EMA）：短期响应更快。",
        "macd": "MACD：趋势与动量综合指标。",
        "macds": "MACD 信号线（Signal）。",
        "macdh": "MACD 柱状图（Histogram）。",
        "rsi": "RSI：衡量超买/超卖的动量指标。",
        "boll": "布林中轨（20 日均线）。",
        "boll_ub": "布林上轨。",
        "boll_lb": "布林下轨。",
        "atr": "ATR：真实波动幅度均值，用于波动与风控。",
        "vwma": "VWMA：成交量加权均线。",
        "obv": "OBV：能量潮指标。",
    }

    @property
    def name(self) -> str:
        return "futu"

    # ── Internal helpers ──

    @staticmethod
    def _to_futu_code(code: str) -> tuple[str, "Market"]:
        """Convert various symbol formats to Futu (market, code) tuple.

        Rules:
          AAPL      → (US, AAPL)     — no suffix, assume US
          NVDA.US   → (US, NVDA)     — explicit US suffix
          00700.HK  → (HK, 000700)   — explicit HK suffix
          商汤       → (HK, 00020)    — Chinese name resolved via stock_resolver
        """
        # Lazy import to avoid hard dependency at module level
        from futu import Market
        from tradingagents.dataflows.stock_resolver import resolve_input

        # Resolve Chinese names / partial codes to canonical symbol
        code = resolve_input(code)
        s = code.strip().upper()

        # Explicit HK suffix (e.g., 00700.HK)
        if s.endswith(".HK"):
            ticker = s[:-3]
            return (Market.HK, ticker)

        # Explicit US suffix (e.g., NVDA.US)
        if s.endswith(".US"):
            ticker = s[:-3]
            return (Market.US, ticker)

        # Futu prefix format: HK.00700
        if s.startswith("HK."):
            return (Market.HK, s[3:])

        # Futu prefix format: US.AAPL
        if s.startswith("US."):
            return (Market.US, s[3:])

        # No suffix → try resolver to detect HK vs US
        try:
            from tradingagents.dataflows.stock_resolver import resolve_ticker
            entry = resolve_ticker(s)
            if entry and entry["market"] == "HK":
                hk_code = entry["code"].replace(".HK", "")
                return (Market.HK, hk_code)
        except ImportError:
            pass
        return (Market.US, s)

    @staticmethod
    def _full_code(market, code: str) -> str:
        """Construct full Futu code like 'US.AAPL' or 'HK.00700'."""
        from futu import Market
        if market == Market.US:
            return f"US.{code}"
        elif market == Market.HK:
            return f"HK.{code}"
        return code

    @staticmethod
    def _need_encrypt() -> bool:
        """Check if RSA encryption is needed (remote host)."""
        host = _opend_host()
        return host not in ("127.0.0.1", "localhost")

    def _get_quote_ctx(self):
        """Create a new Futu OpenQuoteContext.

        Caller MUST close the context when done (use try/finally pattern).
        Encryption is handled globally via SysConfig in _ensure_encrypt().
        """
        from futu import OpenQuoteContext

        self._ensure_encrypt()
        return OpenQuoteContext(host=_opend_host(), port=_opend_port())

    @staticmethod
    def _ensure_encrypt():
        """Set up RSA encryption once (idempotent)."""
        if FutuProvider._need_encrypt() and not FutuProvider._encrypt_done:
            from futu import SysConfig
            rsa_path = os.getenv("FUTU_RSA_KEY_PATH", "config/rsa_key.txt")
            SysConfig.enable_proto_encrypt(is_encrypt=True)
            SysConfig.set_init_rsa_file(rsa_path)
            FutuProvider._encrypt_done = True

    # ── 1.3 get_bars — K 线（返回 List[BarData]，推荐使用） ──

    def get_bars(self, symbol: str, start_date: str, end_date: str,
                 autype: Optional[str] = None) -> list:
        """Fetch historical K-line data as typed BarData objects.

        Args:
            symbol: Stock symbol (e.g., "HK.00700", "AAPL")
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            autype: Adjustment type — None (no adjustment), "qfq" (forward),
                    "hfq" (backward).  Default None.

        Returns list of BarData objects. Empty list if no data.
        """
        from futu import KLType, RET_OK, SubType, AuType
        from tradingagents.models import BarData

        market, code = self._to_futu_code(symbol)
        ctx = self._get_quote_ctx()
        try:
            autype_map = {
                None: AuType.NONE, "qfq": AuType.QFQ, "hfq": AuType.HFQ,
                "NONE": AuType.NONE, "QFQ": AuType.QFQ, "HFQ": AuType.HFQ,
            }
            futu_autype = autype_map.get(autype, AuType.NONE)

            ret, data, _ = ctx.request_history_kline(
                self._full_code(market, code),
                start=start_date, end=end_date,
                ktype=KLType.K_DAY, autype=futu_autype,
            )
            if ret != RET_OK:
                raise RuntimeError(
                    f"Futu API request_history_kline failed for {self._full_code(market, code)}: {ret}"
                )

            if data is None or data.empty:
                return []

            bars = []
            for _, row in data.iterrows():
                bars.append(BarData(
                    symbol=symbol,
                    datetime=pd.to_datetime(row["time_key"]),
                    interval="1d",
                    open_price=float(row["open"]),
                    high_price=float(row["high"]),
                    low_price=float(row["low"]),
                    close_price=float(row["close"]),
                    volume=float(row["volume"]),
                    turnover=float(row.get("turnover", 0)),
                ))
            return bars
        finally:
            ctx.close()

    # ── 1.3b get_stock_data — K 线（返回 CSV 字符串，向后兼容） ──

    def get_stock_data(self, symbol: str, start_date: str, end_date: str,
                       autype: Optional[str] = None) -> str:
        """Fetch historical K-line data as CSV string.

        DEPRECATED: Use get_bars() for type-safe BarData objects.
        Kept for backward compatibility with DataCollector._parse_csv_to_dataframe().
        """
        from tradingagents.models import BarData

        bars = self.get_bars(symbol, start_date, end_date, autype)
        if not bars:
            return ""

        header = f"# Stock data for {symbol} from {start_date} to {end_date}\n"
        header += f"# Total records: {len(bars)}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        csv_lines = [BarData.csv_header()] + [bar.to_csv_row() for bar in bars]
        return header + "\n".join(csv_lines)

    # ── 1.3b get_panel_data — 多股票 panel 格式（Alpha Zoo 用） ──

    def get_panel_data(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        autype: Optional[str] = "qfq",
    ) -> "Dict[str, pd.DataFrame]":
        """Fetch OHLCV data for multiple symbols and return as panel dict.

        Panel format (Alpha Zoo standard):
            {
                "open":   DataFrame(index=DatetimeIndex, columns=symbol),
                "high":   DataFrame(index=DatetimeIndex, columns=symbol),
                "low":    DataFrame(index=DatetimeIndex, columns=symbol),
                "close":  DataFrame(index=DatetimeIndex, columns=symbol),
                "volume": DataFrame(index=DatetimeIndex, columns=symbol),
            }

        Data source priority (P1-5 SQLite cache):
            1. In-memory cache (_PanelCache) — same-day TTL
            2. SQLite cache (tradingagents/data/database.py) — persistent
            3. Futu API — only for missing date ranges

        Args:
            symbols: List of stock symbols (e.g., ["HK.00700", "AAPL"]).
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.
            autype: Adjustment type (default "qfq" for forward adjustment).

        Returns:
            Panel dict with OHLCV DataFrames. Missing symbols are silently skipped.
        """
        # Check in-memory cache first (same-day TTL)
        symbols_tuple = tuple(symbols)
        cached = _panel_cache.get(symbols_tuple, start_date, end_date, autype)
        if cached is not None:
            return cached

        # Try SQLite cache for each symbol
        frames: Dict[str, pd.DataFrame] = {}
        symbols_to_fetch: List[str] = []

        for symbol in symbols:
            canonical = self._to_canonical_symbol(symbol)
            try:
                from tradingagents.data.database import get_bars_as_dataframe, is_cached
                if is_cached(canonical, start_date, end_date):
                    df = get_bars_as_dataframe(canonical, start_date, end_date)
                    if not df.empty:
                        frames[canonical] = df
                        logger.debug("SQLite cache hit for %s (%d bars)", canonical, len(df))
                        continue
            except Exception as e:
                logger.debug("SQLite cache check failed for %s: %s", symbol, e)

            symbols_to_fetch.append(symbol)

        # Fetch missing symbols from Futu API
        if symbols_to_fetch:
            api_frames = self._fetch_from_api(symbols_to_fetch, start_date, end_date, autype)
            frames.update(api_frames)

            # Write fetched data to SQLite cache
            try:
                from tradingagents.data.database import upsert_bars
                for canonical, df in api_frames.items():
                    bars = []
                    for idx, row in df.iterrows():
                        date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                        bars.append({
                            "date": date_str,
                            "open": float(row["open"]) if row["open"] is not None else 0.0,
                            "high": float(row["high"]) if row["high"] is not None else 0.0,
                            "low": float(row["low"]) if row["low"] is not None else 0.0,
                            "close": float(row["close"]) if row["close"] is not None else 0.0,
                            "volume": float(row["volume"]) if row["volume"] is not None else 0.0,
                        })
                    if bars:
                        upsert_bars(canonical, bars, source="futu")
                        logger.debug("Cached %d bars for %s in SQLite", len(bars), canonical)
            except Exception as e:
                logger.warning("Failed to cache data in SQLite: %s", e)

        if not frames:
            return {}

        # Pivot into panel format
        panel: Dict[str, pd.DataFrame] = {}
        for col in ("open", "high", "low", "close", "volume"):
            panel[col] = pd.DataFrame(
                {sym: frames[sym][col] for sym in frames},
                index=frames[list(frames.keys())[0]].index,
            )

        # Cache in memory
        _panel_cache.put(symbols_tuple, start_date, end_date, autype, panel)

        # Validate OHLC data quality (warn on issues, don't block)
        for sym, close_df in panel.get("close", pd.DataFrame()).items():
            sym_df = pd.DataFrame({col: panel[col][sym] for col in panel if sym in panel[col]})
            self._validate_ohlc(sym_df, strategy="warn")

        return panel

    def _to_canonical_symbol(self, symbol: str) -> str:
        """Convert any symbol format to TAF canonical (e.g., 'HK.00700' → '00700.HK').

        Uses stock_resolver for proper normalization.
        """
        from tradingagents.dataflows.stock_resolver import to_canonical
        return to_canonical(symbol)

    def _fetch_from_api(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        autype: Optional[str],
    ) -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV data from Futu API for given symbols.

        Returns:
            Dict mapping canonical_symbol → DataFrame with OHLCV columns.
        """
        from futu import KLType, RET_OK, AuType

        autype_map = {
            None: AuType.NONE,
            "qfq": AuType.QFQ,
            "hfq": AuType.HFQ,
            "NONE": AuType.NONE,
            "QFQ": AuType.QFQ,
            "HFQ": AuType.HFQ,
        }
        futu_autype = autype_map.get(autype, AuType.NONE)

        frames: Dict[str, pd.DataFrame] = {}
        ctx = self._get_quote_ctx()
        try:
            for symbol in symbols:
                try:
                    market, code = self._to_futu_code(symbol)
                    ret, data, _ = ctx.request_history_kline(
                        self._full_code(market, code),
                        start=start_date,
                        end=end_date,
                        ktype=KLType.K_DAY,
                        autype=futu_autype,
                        max_count=10_000,
                    )
                    if ret != RET_OK or data is None or data.empty:
                        continue

                    df = data.rename(columns={
                        "time_key": "Date",
                        "open": "open",
                        "high": "high",
                        "low": "low",
                        "close": "close",
                        "volume": "volume",
                    })
                    df["Date"] = pd.to_datetime(df["Date"])
                    df = df.set_index("Date").sort_index()
                    canonical = self._to_canonical_symbol(symbol)
                    frames[canonical] = df[["open", "high", "low", "close", "volume"]].astype(float)
                except Exception as e:
                    logger.debug("API fetch failed for %s: %s", symbol, e)
                    continue
        finally:
            ctx.close()

        return frames

    # ── OHLC Data Validation ──

    @staticmethod
    def _validate_ohlc(
        df: pd.DataFrame,
        *,
        strategy: str = "warn",
        price_jump_threshold: float = 0.5,
    ) -> Dict[str, Any]:
        """Validate OHLC data quality and return diagnostics.

        Checks for common data issues that can corrupt backtest results:
        - Missing values (NaN)
        - Invalid prices (close <= 0, high < low)
        - Zero volume bars (may indicate delisted/suspended stocks)
        - Price jumps > threshold (vs previous day, default 50%)

        Args:
            df: DataFrame with columns [open, high, low, close, volume].
                Index should be DatetimeIndex for time-series analysis.
            strategy: How to handle violations:
                - "warn": Log warnings, return diagnostics (default)
                - "drop": Return cleaned DataFrame with violations removed
                - "raise": Raise ValueError on any violation
            price_jump_threshold: Max allowed daily price change ratio (default 0.5 = 50%).
                Set lower (e.g. 20%) for penny stocks or pre-split scenarios.

        Returns:
            Dict with keys:
                - "is_valid": bool, True if no violations found
                - "violations": dict mapping violation type -> list of affected dates
                - "stats": dict with summary stats (total_bars, nan_count, etc.)
                - "cleaned_df": DataFrame with violations handled per strategy
                - "repair_suggestions": list of human-readable repair suggestions

        Example:
            >>> result = FutuProvider._validate_ohlc(df)
            >>> if not result["is_valid"]:
            ...     print(f"Found {len(result['violations'])} issues")
            ...     for vtype, dates in result["violations"].items():
            ...         print(f"  {vtype}: {len(dates)} bars")
        """
        required_cols = ("open", "high", "low", "close")
        if df.empty or not all(col in df.columns for col in required_cols):
            return {
                "is_valid": True,
                "violations": {},
                "stats": {"total_bars": 0},
                "cleaned_df": df,
                "repair_suggestions": [],
            }

        violations: Dict[str, list] = {}
        repair_suggestions = []
        mask_valid = pd.Series(True, index=df.index)

        # 1. Missing values (NaN)
        nan_mask = df[list(required_cols)].isnull().any(axis=1)
        if "volume" in df.columns:
            nan_mask = nan_mask | df["volume"].isnull()
        nan_dates = df.index[nan_mask].tolist()
        if nan_dates:
            violations["nan_values"] = [str(d) for d in nan_dates]
            repair_suggestions.append(
                f"Found {len(nan_dates)} bars with NaN values. "
                "Consider forward-fill (ffill) or interpolation for gaps."
            )
            if strategy == "drop":
                mask_valid = mask_valid & ~nan_mask

        # 2. Invalid prices (close <= 0 or high < low)
        invalid_price_mask = (
            (df["close"] <= 0)
            | (df["open"] <= 0)
            | (df["high"] <= 0)
            | (df["low"] <= 0)
            | (df["high"] < df["low"])
        )
        invalid_price_dates = df.index[invalid_price_mask].tolist()
        if invalid_price_dates:
            violations["invalid_prices"] = [str(d) for d in invalid_price_dates]
            repair_suggestions.append(
                f"Found {len(invalid_price_dates)} bars with invalid prices "
                "(close<=0 or high<low). These are likely data errors — consider removing."
            )
            if strategy == "drop":
                mask_valid = mask_valid & ~invalid_price_mask

        # 3. Zero volume bars
        if "volume" in df.columns:
            zero_vol_mask = df["volume"] == 0
            zero_vol_dates = df.index[zero_vol_mask].tolist()
            if zero_vol_dates:
                violations["zero_volume"] = [str(d) for d in zero_vol_dates]
                repair_suggestions.append(
                    f"Found {len(zero_vol_dates)} bars with zero volume. "
                    "May indicate suspension or delisting — verify manually."
                )
                # Don't auto-drop zero volume bars (they may be valid for suspended stocks)

        # 4. Price jumps (> threshold vs previous day)
        if len(df) > 1:
            close_series = df["close"].astype(float)
            pct_change = close_series.pct_change().abs()
            jump_mask = pct_change > price_jump_threshold
            jump_dates = df.index[jump_mask].tolist()
            if jump_dates:
                violations["price_jumps"] = [str(d) for d in jump_dates]
                repair_suggestions.append(
                    f"Found {len(jump_dates)} bars with price jumps > {price_jump_threshold*100:.0f}%. "
                    "May indicate stock splits, dividends, or data errors."
                )
                if strategy == "drop":
                    mask_valid = mask_valid & ~jump_mask

        # Build result
        is_valid = len(violations) == 0
        cleaned_df = df[mask_valid] if strategy == "drop" else df

        stats = {
            "total_bars": len(df),
            "nan_count": len(violations.get("nan_values", [])),
            "invalid_price_count": len(violations.get("invalid_prices", [])),
            "zero_volume_count": len(violations.get("zero_volume", [])),
            "price_jump_count": len(violations.get("price_jumps", [])),
            "valid_bars": int(mask_valid.sum()),
            "dropped_bars": len(df) - int(mask_valid.sum()) if strategy == "drop" else 0,
        }

        if not is_valid:
            total_issues = sum(len(v) for v in violations.values())
            if strategy == "raise":
                raise ValueError(
                    f"OHLC validation failed: {total_issues} violations found. "
                    f"Types: {list(violations.keys())}"
                )
            elif strategy == "warn":
                logger.warning(
                    "OHLC validation: %d violations in %d bars (%s)",
                    total_issues, len(df), list(violations.keys())
                )

        return {
            "is_valid": is_valid,
            "violations": violations,
            "stats": stats,
            "cleaned_df": cleaned_df,
            "repair_suggestions": repair_suggestions,
        }

    # ── 1.4 get_indicators — 技术指标 ──

    def get_indicators(
        self, symbol: str, indicator: str, curr_date: str, look_back_days: int
    ) -> str:
        """Compute technical indicators using stockstats over Futu K-line data.

        Reuses the same indicator set as yfinance providers.
        """
        if indicator not in self.INDICATOR_DESCRIPTIONS:
            raise ValueError(
                f"Indicator {indicator} is not supported. "
                f"Please choose from: {list(self.INDICATOR_DESCRIPTIONS.keys())}"
            )

        from futu import KLType, RET_OK, AuType

        market, code = self._to_futu_code(symbol)
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        # Fetch enough history for indicator warmup (260 days covers 200-SMA)
        start_dt = curr_dt - timedelta(days=max(look_back_days, 260))

        ctx = self._get_quote_ctx()
        try:
            ret, data, _ = ctx.request_history_kline(
                self._full_code(market, code),
                start=start_dt.strftime("%Y-%m-%d"),
                end=curr_date,
                ktype=KLType.K_DAY,
                autype=AuType.NONE,
            )
            if ret != RET_OK:
                raise RuntimeError(
                    f"Futu API request_history_kline failed for {self._full_code(market, code)} "
                    f"(indicator {indicator}): {ret}"
                )

            if data is None or data.empty:
                return f"No data found for {symbol} for indicator {indicator}"

            # Prepare DataFrame for stockstats (expects lowercase column names)
            ind_df = data.rename(
                columns={
                    "time_key": "date",
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "close": "close",
                    "volume": "volume",
                }
            )[["date", "open", "high", "low", "close", "volume"]].copy()

            ind_df["date"] = pd.to_datetime(ind_df["date"], errors="coerce")
            ind_df = (
                ind_df.dropna(subset=["date"])
                .sort_values("date")
                .reset_index(drop=True)
            )

            ss = wrap(ind_df)
            indicator_series = ss[indicator]

            # Build date→value map
            values_by_date = {}
            for idx, dt_val in enumerate(ind_df["date"]):
                date_str = pd.to_datetime(dt_val).strftime("%Y-%m-%d")
                val = indicator_series.iloc[idx]
                values_by_date[date_str] = "N/A" if pd.isna(val) else str(val)

            begin = curr_dt - timedelta(days=look_back_days)
            lines = []
            d = curr_dt
            while d >= begin:
                key = d.strftime("%Y-%m-%d")
                value = values_by_date.get(key, "N/A")
                lines.append(f"{key}: {value}")
                d -= timedelta(days=1)

            result = (
                f"## {indicator} 指标值（{begin.strftime('%Y-%m-%d')} 至 {curr_date}）：\n\n"
                + "\n".join(lines)
                + "\n\n"
                + self.INDICATOR_DESCRIPTIONS[indicator]
            )
            return result
        finally:
            ctx.close()

    # ── 1.5 get_fundamentals — 公司概况 ──

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        """Fetch company snapshot (PE, PB, market cap, etc.) via FutuOpenD.

        Returns a Markdown table with key fundamental metrics.
        """
        from futu import RET_OK

        market, code = self._to_futu_code(ticker)
        full = self._full_code(market, code)
        ctx = self._get_quote_ctx()
        try:
            ret, data = ctx.get_market_snapshot([full])
            if ret != RET_OK:
                raise RuntimeError(
                    f"Futu API get_market_snapshot failed for {full}: {ret}"
                )

            if data is None or data.empty:
                return f"No fundamental data found for {full}"

            row = data.iloc[0]

            # Extract key metrics — Futu snapshot provides rich fundamental data
            table_rows = [
                ("股票代码", code),
                ("股票名称", row.get("name", "N/A")),
                ("最新价", row.get("last_price", "N/A")),
                ("涨跌幅", f"{row.get('price_spread', 'N/A')}%"),
                ("市盈率 (PE)", row.get("pe_ttm_ratio", "N/A")),
                ("市净率 (PB)", row.get("pb_ratio", "N/A")),
                ("总市值", row.get("market_val", "N/A")),
                ("股息率", f"{row.get('dividend_ratio_ttm', 'N/A')}%"),
                ("换手率", f"{row.get('turnover_rate', 'N/A')}%"),
                ("振幅", f"{row.get('amplitude', 'N/A')}%"),
                ("52周最高", row.get("high_price", "N/A")),
                ("52周最低", row.get("low_price", "N/A")),
                ("成交量", row.get("volume", "N/A")),
                ("成交额", row.get("turnover", "N/A")),
            ]

            md = f"## {ticker} 基本面数据\n\n"
            md += "| 指标 | 数值 |\n"
            md += "|------|------|\n"
            for label, value in table_rows:
                md += f"| {label} | {value} |\n"
            return md
        finally:
            ctx.close()

    def get_fundamentals_dict(self, ticker: str) -> Dict[str, Any]:
        """Fetch company snapshot as a raw dict (for programmatic use).

        Returns dict with keys: pe_ttm, pb_ratio, market_cap, dividend_ratio,
        turnover_rate, amplitude, high_52w, low_52w, volume, turnover, name, price.
        """
        from futu import RET_OK

        market, code = self._to_futu_code(ticker)
        full = self._full_code(market, code)
        ctx = self._get_quote_ctx()
        try:
            ret, data = ctx.get_market_snapshot([full])
            if ret != RET_OK or data is None or data.empty:
                logger.warning("get_fundamentals_dict failed for %s: %s", full, ret)
                return {}

            row = data.iloc[0]
            return {
                "name": str(row.get("name", "")),
                "price": float(row.get("last_price", 0) or 0),
                "pe_ttm": float(row.get("pe_ttm_ratio", 0) or 0),
                "pb_ratio": float(row.get("pb_ratio", 0) or 0),
                "market_cap": float(row.get("market_val", 0) or 0),
                "dividend_ratio": float(row.get("dividend_ratio_ttm", 0) or 0),
                "turnover_rate": float(row.get("turnover_rate", 0) or 0),
                "amplitude": float(row.get("amplitude", 0) or 0),
                "high_52w": float(row.get("high_price", 0) or 0),
                "low_52w": float(row.get("low_price", 0) or 0),
                "volume": int(row.get("volume", 0) or 0),
                "turnover": float(row.get("turnover", 0) or 0),
            }
        finally:
            ctx.close()

    # ── Financial statements (balance sheet / cashflow / income) ──
    # Futu API does not provide detailed financial statements via snapshot.
    # These methods raise NotImplementedError so the fallback chain continues.

    def get_balance_sheet(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        raise NotImplementedError("Futu does not provide balance sheet data")

    def get_cashflow(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        raise NotImplementedError("Futu does not provide cashflow data")

    def get_income_statement(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        raise NotImplementedError("Futu does not provide income statement data")

    # ── News (not available via Futu) ──

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        raise NotImplementedError("Futu does not provide news data")

    def get_global_news(
        self, curr_date: str, look_back_days: int = 7, limit: int = 50
    ) -> str:
        raise NotImplementedError("Futu does not provide global news data")

    def get_insider_transactions(self, symbol: str) -> str:
        raise NotImplementedError("Futu does not provide insider transaction data")

    # ── 1.6 get_realtime_quotes — 实时行情 ──

    def get_realtime_quotes(self, symbols: list[str]) -> str:
        """Fetch real-time quotes for a batch of symbols via FutuOpenD.

        Returns a CSV string with columns:
        symbol,price,change,change_pct,volume,high,low,open
        Supports batch queries (single API call for all symbols).
        """
        from futu import RET_OK

        if not symbols:
            return ""

        # Convert all symbols to Futu codes
        code_map = {}  # futu_code → original_symbol
        for sym in symbols:
            try:
                market, code = self._to_futu_code(sym)
                full = self._full_code(market, code)
                code_map[full] = sym
            except NotImplementedError:
                # Skip unsupported symbols (e.g., A-share)
                continue

        if not code_map:
            return ""

        ctx = self._get_quote_ctx()
        try:
            ret, data = ctx.get_market_snapshot(list(code_map.keys()))
            if ret != RET_OK:
                raise RuntimeError(
                    f"Futu API get_market_snapshot failed for quotes: {ret}"
                )

            if data is None or data.empty:
                return ""  # No data available is not an error — return empty

            rows = []
            for _, row in data.iterrows():
                futu_code = row.get("code", "")
                original_sym = code_map.get(futu_code, futu_code)
                last_price = row.get("last_price", 0)
                prev_close = row.get("prev_close_price", 0)
                change = last_price - prev_close if prev_close else 0
                change_pct = (
                    (change / prev_close * 100) if prev_close else 0
                )
                rows.append(
                    {
                        "symbol": original_sym,
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
                        "lot_size": int(row.get("lot_size", 0) or 0),
                        "sec_status": str(row.get("sec_status", "") or ""),
                    }
                )

            result_df = pd.DataFrame(rows)
            return result_df.to_csv(index=False)
        finally:
            ctx.close()

    def get_positions(self) -> list[dict[str, Any]]:
        """Fetch real portfolio positions from FutuOpenD.

        Returns list of dicts with keys:
            code, stock_name, qty, can_sell_qty, cost_price, average_cost,
            market_val, pl_ratio, pl_val, currency, position_side
        """
        try:
            from futu import OpenSecTradeContext, TrdEnv, SecurityFirm
        except ImportError:
            logger.warning("[futu] futu-api not installed, cannot fetch positions")
            return []

        ctx = None
        try:
            ctx = OpenSecTradeContext(
                host=_opend_host(),
                port=_opend_port(),
                security_firm=SecurityFirm.FUTUSECURITIES,
            )
            ret, data = ctx.position_list_query(trd_env=TrdEnv.REAL)
            if ret != 0:
                logger.warning("[futu] position_list_query failed: %s", ret)
                return []
            if data is None or data.empty:
                return []

            positions = []
            codes_for_snapshot = []
            for _, row in data.iterrows():
                qty = row.get("qty", 0)
                if qty <= 0:
                    continue
                # Convert Futu code (HK.00700) to canonical (00700.HK)
                futu_code = row.get("code", "")
                canonical = _futu_code_to_canonical(futu_code)
                codes_for_snapshot.append(futu_code)
                positions.append({
                    "symbol": canonical,
                    "futu_code": futu_code,
                    "stock_name": row.get("stock_name", ""),
                    "qty": float(qty),
                    "can_sell_qty": float(row.get("can_sell_qty", 0)),
                    "cost_price": float(row.get("cost_price", 0)),
                    "average_cost": float(row.get("average_cost", 0)),
                    "market_val": float(row.get("market_val", 0)),
                    "nominal_price": float(row.get("nominal_price", 0)),
                    "pl_ratio": float(row.get("pl_ratio", 0)),
                    "pl_val": float(row.get("pl_val", 0)),
                    "today_pl_val": float(row.get("today_pl_val", 0)),
                    "unrealized_pl": float(row.get("unrealized_pl", 0)),
                    "realized_pl": float(row.get("realized_pl", 0)),
                    "currency": row.get("currency", ""),
                    "position_side": row.get("position_side", "LONG"),
                    "lot_size": 0,
                })

            # Fetch lot_size from quote snapshot
            if codes_for_snapshot:
                try:
                    from futu import OpenQuoteContext
                    qctx = OpenQuoteContext(
                        host=_opend_host(), port=_opend_port(),
                        security_firm=SecurityFirm.FUTUSECURITIES,
                    )
                    try:
                        ret2, snap = qctx.get_market_snapshot(codes_for_snapshot)
                        if ret2 == 0 and snap is not None and not snap.empty:
                            lot_map = {}
                            for _, sr in snap.iterrows():
                                lot_map[str(sr.get("code", ""))] = int(sr.get("lot_size", 0) or 0)
                            for p in positions:
                                p["lot_size"] = lot_map.get(p["futu_code"], 0)
                    finally:
                        qctx.close()
                except Exception:
                    pass

            return positions
        except Exception as exc:
            logger.warning("[futu] get_positions failed: %s", exc)
            return []
        finally:
            if ctx:
                ctx.close()


from tradingagents.dataflows.stock_resolver import to_futu as _to_futu_format


def _canonical_to_futu(code: str) -> str:
    """Convert any code format to Futu format (MARKET.CODE).

    Use stock_resolver.to_futu() directly for new code.
    """
    return _to_futu_format(code)


def get_market_state(symbols: list[str]) -> dict[str, str]:
    """Get market state for a list of canonical symbols.

    Returns dict mapping canonical symbol -> market state string
    (e.g. 'TRADING', 'CLOSED', 'PRE_MARKET', 'AFTER_HOURS', etc.)
    """
    if not symbols:
        return {}
    try:
        from futu import OpenQuoteContext, SecurityFirm
    except ImportError:
        return {}

    ctx = None
    try:
        ctx = OpenQuoteContext(
            host=_opend_host(),
            port=_opend_port(),
            security_firm=SecurityFirm.FUTUSECURITIES,
        )
        futu_codes = [_canonical_to_futu(s) for s in symbols]
        result = {}
        code_to_canonical = {}
        for i in range(0, len(futu_codes), 100):
            batch = futu_codes[i:i+100]
            for fc, sym in zip(batch, symbols[i:i+100]):
                code_to_canonical[fc] = sym
            ret, data = ctx.get_market_state(batch)
            if ret != 0:
                continue
            for _, row in data.iterrows():
                fc = row.get("code", "")
                canonical = code_to_canonical.get(fc, fc)
                result[canonical] = row.get("market_state", "")
        return result
    except Exception as exc:
        logger.warning("[futu] get_market_state failed: %s", exc)
        return {}
    finally:
        if ctx:
            ctx.close()


def _futu_code_to_canonical(futu_code: str) -> str:
    """Convert Futu code (HK.00700, US.AAPL) to canonical (00700.HK, AAPL)."""
    if "." not in futu_code:
        return futu_code
    parts = futu_code.split(".", 1)
    if len(parts) != 2:
        return futu_code
    market, code = parts
    if market == "HK":
        return f"{code}.HK"
    elif market == "US":
        return code  # US tickers are bare
    return futu_code
