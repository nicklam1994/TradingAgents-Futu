"""DataLoader Protocol for TradingAgents-Futu.

Unified data loading interface with Futu OpenD as primary source.
Based on Vibe-Trading agent/backtest/loaders/base.py, adapted for TAF.

Phase 13.3: DataLoader Protocol — 统一数据加载 + fallback

Usage:
    from tradingagents.data.loaders import DataLoaderProtocol, FutuLoader, LoaderRegistry

    # Register loaders
    registry = LoaderRegistry()
    registry.register(FutuLoader())

    # Fetch data with automatic fallback
    data = registry.fetch(["HK.00700", "AAPL"], "2026-01-01", "2026-06-24")
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional, Protocol, runtime_checkable

import pandas as pd

from tradingagents.models.constant import Market, parse_market

logger = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────────

class NoAvailableSourceError(Exception):
    """Raised when no data source is available for a given market."""


# ── Validation helpers ───────────────────────────────────────────────────────

def validate_date_range(start_date: str, end_date: str) -> None:
    """Validate that start_date <= end_date."""
    try:
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
    except Exception as exc:
        raise ValueError(f"Invalid date format: start={start_date!r}, end={end_date!r}") from exc
    if start > end:
        raise ValueError(f"start_date ({start_date}) > end_date ({end_date})")


def validate_ohlc(frame: pd.DataFrame, *, strategy: str = "drop") -> pd.DataFrame:
    """Drop, flag, or reject bars that violate OHLC invariants.

    Args:
        frame: OHLCV frame with at least open/high/low/close columns.
        strategy: "drop" (remove invalid rows), "warn" (log and keep), or "raise".

    Returns:
        The frame with invalid rows removed or unchanged.
    """
    required = ("open", "high", "low", "close")
    if frame.empty or not all(col in frame.columns for col in required):
        return frame

    open_, high, low, close = (frame[c] for c in required)
    invalid = (
        (high < low)
        | (high < open_)
        | (high < close)
        | (low > open_)
        | (low > close)
        | (open_ <= 0)
        | (high <= 0)
        | (low <= 0)
        | (close <= 0)
    )
    n_invalid = int(invalid.sum())
    if n_invalid == 0:
        return frame

    if strategy == "raise":
        raise ValueError(f"{n_invalid} bar(s) violate OHLC invariants")
    if strategy == "warn":
        logger.warning("OHLC validation: %d bar(s) violate invariants (kept)", n_invalid)
        return frame
    logger.warning("OHLC validation: dropping %d invalid bar(s)", n_invalid)
    return frame[~invalid]


# ── DataLoader Protocol ──────────────────────────────────────────────────────

@runtime_checkable
class DataLoaderProtocol(Protocol):
    """Interface that every data source loader must satisfy."""

    name: str
    markets: set[str]
    requires_auth: bool

    def is_available(self) -> bool:
        """Check whether this data source is usable."""
        ...

    def fetch(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[list[str]] = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV data.

        Returns:
            Mapping {symbol: DataFrame(date, open, high, low, close, volume)}.
        """
        ...


# ── Base Loader (for subclassing) ───────────────────────────────────────────

class BaseLoader(ABC):
    """Base class for data loaders with common utilities."""

    def __init__(self, name: str, markets: set[str], requires_auth: bool = False):
        self.name = name
        self.markets = markets
        self.requires_auth = requires_auth

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this loader is usable."""
        ...

    @abstractmethod
    def fetch(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[list[str]] = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV data."""
        ...

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol to standard format (HK.XXXXX or XXXXX.US)."""
        symbol = symbol.strip().upper()
        if symbol.startswith("HK.") or symbol.endswith(".HK"):
            return symbol if symbol.startswith("HK.") else f"HK.{symbol.replace('.HK', '')}"
        elif symbol.startswith("US.") or symbol.endswith(".US"):
            return symbol if symbol.startswith("US.") else f"US.{symbol.replace('.US', '')}"
        return symbol


# ── Loader Registry ──────────────────────────────────────────────────────────

class LoaderRegistry:
    """Registry for data loaders with automatic fallback."""

    def __init__(self):
        self._loaders: dict[str, BaseLoader] = {}
        self._market_loaders: dict[str, list[str]] = {}  # market -> [loader_names]

    def register(self, loader: BaseLoader) -> None:
        """Register a loader."""
        self._loaders[loader.name] = loader
        for market in loader.markets:
            if market not in self._market_loaders:
                self._market_loaders[market] = []
            if loader.name not in self._market_loaders[market]:
                self._market_loaders[market].append(loader.name)

    def get_loader(self, name: str) -> Optional[BaseLoader]:
        """Get a loader by name."""
        return self._loaders.get(name)

    def list_loaders(self) -> list[str]:
        """List all registered loader names."""
        return list(self._loaders.keys())

    def get_loaders_for_market(self, market: str) -> list[BaseLoader]:
        """Get all available loaders for a market, in priority order."""
        loader_names = self._market_loaders.get(market, [])
        return [
            self._loaders[name]
            for name in loader_names
            if self._loaders[name].is_available()
        ]

    def fetch(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[list[str]] = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch data with automatic fallback across loaders.

        Groups codes by market, tries loaders in priority order.

        Returns:
            Mapping {symbol: DataFrame} for successfully fetched symbols.
        """
        validate_date_range(start_date, end_date)

        # Group codes by market
        market_codes: dict[str, list[str]] = {}
        for code in codes:
            market = parse_market(code).value
            if market not in market_codes:
                market_codes[market] = []
            market_codes[market].append(code)

        result: dict[str, pd.DataFrame] = {}

        for market, market_symbols in market_codes.items():
            loaders = self.get_loaders_for_market(market)
            if not loaders:
                logger.warning(f"No loaders available for market {market}")
                continue

            remaining = set(market_symbols)
            for loader in loaders:
                if not remaining:
                    break
                try:
                    data = loader.fetch(
                        list(remaining),
                        start_date,
                        end_date,
                        interval=interval,
                        fields=fields,
                    )
                    for symbol, df in data.items():
                        if not df.empty:
                            df = validate_ohlc(df, strategy="drop")
                            if not df.empty:
                                result[symbol] = df
                                remaining.discard(symbol)
                except Exception as exc:
                    logger.warning(f"Loader {loader.name} failed for {market}: {exc}")

            if remaining:
                logger.warning(f"No data for {len(remaining)} symbols in {market}: {remaining}")

        return result
