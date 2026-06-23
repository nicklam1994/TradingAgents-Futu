"""FutuLoader — Futu OpenD data loader for TradingAgents-Futu.

Wraps the existing FutuProvider to implement DataLoaderProtocol.
Based on Vibe-Trading agent/backtest/loaders/futu.py, adapted for TAF.

Phase 13.3: FutuLoader for DataLoader Protocol
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from tradingagents.data.loaders import BaseLoader
from tradingagents.models.constant import Market, parse_market

logger = logging.getLogger(__name__)


class FutuLoader(BaseLoader):
    """Futu OpenD data loader.

    Fetches OHLCV data via FutuOpenD API.
    Supports HK and US markets.
    """

    def __init__(self):
        super().__init__(
            name="futu",
            markets={"HK", "US"},
            requires_auth=True,
        )

    def is_available(self) -> bool:
        """Check if FutuOpenD is available."""
        try:
            from futu import OpenQuoteContext, RET_OK
            ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
            ret, _ = ctx.get_global_state()
            ctx.close()
            return ret == RET_OK
        except Exception:
            return False

    def fetch(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[list[str]] = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV data from FutuOpenD.

        Args:
            codes: List of symbols (e.g., ["HK.00700", "AAPL"]).
            start_date: Start date (YYYY-MM-DD).
            end_date: End date (YYYY-MM-DD).
            interval: K-line interval ("1D", "1H", "1M", etc.).

        Returns:
            Mapping {symbol: DataFrame(date, open, high, low, close, volume)}.
        """
        from futu import KLType, RET_OK, OpenQuoteContext

        # Map interval to Futu KLType
        interval_map = {
            "1D": KLType.K_DAY,
            "1d": KLType.K_DAY,
            "1H": KLType.K_60M,
            "1h": KLType.K_60M,
            "1M": KLType.K_1M,
            "1m": KLType.K_1M,
        }
        kl_type = interval_map.get(interval, KLType.K_DAY)

        result: dict[str, pd.DataFrame] = {}
        ctx = OpenQuoteContext(host=self._get_host(), port=self._get_port())

        try:
            for symbol in codes:
                try:
                    futu_code = self._to_futu_code(symbol)
                    ret, data, _ = ctx.request_history_kline(
                        futu_code,
                        start=start_date,
                        end=end_date,
                        ktype=kl_type,
                    )
                    if ret != RET_OK:
                        logger.warning(f"Futu kline failed for {symbol}: {data}")
                        continue

                    if data is None or data.empty:
                        continue

                    # Normalize columns
                    df = data.rename(columns={
                        "time_key": "date",
                        "open": "open",
                        "high": "high",
                        "low": "low",
                        "close": "close",
                        "volume": "volume",
                        "turnover": "turnover",
                    })
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.set_index("date")
                    df = df[["open", "high", "low", "close", "volume"]].astype(float)
                    result[symbol] = df

                except Exception as exc:
                    logger.warning(f"Failed to fetch {symbol} from Futu: {exc}")

        finally:
            ctx.close()

        return result

    def _to_futu_code(self, symbol: str) -> str:
        """Convert symbol to Futu format (HK.XXXXX or US.XXXX)."""
        symbol = symbol.strip().upper()

        # Already in Futu format
        if symbol.startswith("HK.") or symbol.startswith("US."):
            return symbol

        # Convert from other formats
        if symbol.endswith(".HK"):
            code = symbol.replace(".HK", "")
            return f"HK.{code}"
        elif symbol.endswith(".US"):
            code = symbol.replace(".US", "")
            return f"US.{code}"

        # Ambiguous - try to infer
        market = parse_market(symbol)
        if market == Market.HK:
            return f"HK.{symbol}"
        else:
            return f"US.{symbol}"

    def _get_host(self) -> str:
        """Get FutuOpenD host."""
        import os
        return os.getenv("FUTU_OPEND_HOST", "127.0.0.1")

    def _get_port(self) -> int:
        """Get FutuOpenD port."""
        import os
        return int(os.getenv("FUTU_OPEND_PORT", "11111"))
