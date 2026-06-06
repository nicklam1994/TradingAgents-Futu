"""Futu Provider — US/HK market data via FutuOpenD API.

Requires a running FutuOpenD instance (default: 127.0.0.1:11111).
Configure via FUTU_OPEND_HOST / FUTU_OPEND_PORT environment variables.
"""

import json
import logging
import os
from typing import Any, Dict, Optional
from datetime import datetime, timedelta

import pandas as pd
from stockstats import wrap

from .base import BaseMarketDataProvider

logger = logging.getLogger(__name__)


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
          00700.HK  → (HK, 00700)    — explicit HK suffix
          600519.SH → NotImplementedError (A-share not supported by Futu)
          000001.SZ → NotImplementedError
        """
        # Lazy import to avoid hard dependency at module level
        from futu import Market

        s = code.strip().upper()

        # Explicit A-share suffixes → reject
        if s.endswith(".SH") or s.endswith(".SZ"):
            raise NotImplementedError(
                f"Futu does not support A-share symbols: {code}. "
                "Use yfinance for SH/SZ stocks."
            )

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

    # ── 1.3 get_stock_data — K 线 ──

    def get_stock_data(self, symbol: str, start_date: str, end_date: str,
                       autype: Optional[str] = None) -> str:
        """Fetch historical K-line data via FutuOpenD.

        Args:
            symbol: Stock symbol (e.g., "HK.00700", "AAPL")
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            autype: Adjustment type — None (no adjustment), "qfq" (forward),
                    "hfq" (backward).  Default None.

        Returns CSV string with columns: Date,Open,High,Low,Close,Volume.
        """
        from futu import KLType, RET_OK, SubType, AuType

        market, code = self._to_futu_code(symbol)
        ctx = self._get_quote_ctx()
        try:
            # Map autype string to Futu AuType enum
            autype_map = {
                None: AuType.NONE,
                "qfq": AuType.QFQ,
                "hfq": AuType.HFQ,
                "NONE": AuType.NONE,
                "QFQ": AuType.QFQ,
                "HFQ": AuType.HFQ,
            }
            futu_autype = autype_map.get(autype, AuType.NONE)

            ret, data, _ = ctx.request_history_kline(
                self._full_code(market, code),
                start=start_date,
                end=end_date,
                ktype=KLType.K_DAY,
                autype=futu_autype,
            )
            if ret != RET_OK:
                raise RuntimeError(
                    f"Futu API request_history_kline failed for {self._full_code(market, code)}: {ret}"
                )

            if data is None or data.empty:
                return ""  # No data available is not an error — return empty

            # Normalize column names to match the expected CSV format
            df = data.rename(
                columns={
                    "time_key": "Date",
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )
            # Keep only the standard columns
            cols = ["Date", "Open", "High", "Low", "Close", "Volume"]
            df = df[cols].copy()
            df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")

            header = f"# Stock data for {symbol} from {start_date} to {end_date}\n"
            header += f"# Total records: {len(df)}\n"
            header += (
                f"# Data retrieved on: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + df.to_csv(index=False)
        finally:
            ctx.close()

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
            for _, row in data.iterrows():
                qty = row.get("qty", 0)
                if qty <= 0:
                    continue
                # Convert Futu code (HK.00700) to canonical (00700.HK)
                futu_code = row.get("code", "")
                canonical = _futu_code_to_canonical(futu_code)
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
                    "unrealized_pl": float(row.get("unrealized_pl", 0)),
                    "realized_pl": float(row.get("realized_pl", 0)),
                    "currency": row.get("currency", ""),
                    "position_side": row.get("position_side", "LONG"),
                })
            return positions
        except Exception as exc:
            logger.warning("[futu] get_positions failed: %s", exc)
            return []
        finally:
            if ctx:
                ctx.close()


def _canonical_to_futu(code: str) -> str:
    """Convert canonical code (00700.HK, AAPL) to Futu format (HK.00700, US.AAPL)."""
    if code.endswith(".HK"):
        return f"HK.{code[:-3]}"
    # Numeric-only codes are HK (stock_resolver stores them without .HK)
    if code.isdigit():
        return f"HK.{code}"
    return f"US.{code}"


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
