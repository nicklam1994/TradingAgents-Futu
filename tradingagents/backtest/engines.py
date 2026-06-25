"""Multi-engine backtest for TradingAgents-Futu.

Phase 13.4: Multi-engine backtest — 佣金/印花税/滑点
Phase 13.5: Refactored to use enhanced BaseEngine + GlobalEquityEngine from new modules.

This module provides the API-facing adapter layer:
  - Simple BaseEngine (4 abstract methods, no execution loop) for lightweight use
  - CompositeEngine for auto-routing by symbol prefix (HK./US.)
  - create_engine() factory with auto-detection
  - get_commission_rate() convenience function

For the full bar-by-bar execution loop, use:
  - tradingagents.backtest.base_engine.BaseEngine
  - tradingagents.backtest.global_equity_engine.GlobalEquityEngine
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


# ── Simple BaseEngine (API-facing, no execution loop) ────────────────────────

class BaseEngine(ABC):
    """Base class for market-specific backtest engines.

    Subclasses must implement the 4 abstract methods that define
    market-specific trading rules. This is the lightweight interface
    used by the API layer (strategy_analytics_service.py).

    For the full bar-by-bar execution loop with signal alignment,
    use tradingagents.backtest.base_engine.BaseEngine instead.
    """

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def can_execute(self, symbol: str, direction: int, price: float) -> bool:
        """Check if an order can be executed.

        Args:
            symbol: Stock symbol.
            direction: 1 for buy, -1 for sell.
            price: Current price.

        Returns:
            True if order can be executed.
        """
        ...

    @abstractmethod
    def round_size(self, raw_size: float, price: float) -> float:
        """Round order size to valid lot size.

        Args:
            raw_size: Desired shares.
            price: Current price.

        Returns:
            Rounded shares (e.g., 100 for HK, 0.01 for US).
        """
        ...

    @abstractmethod
    def calc_commission(self, size: float, price: float, direction: int, is_open: bool) -> float:
        """Calculate trading commission.

        Args:
            size: Number of shares.
            price: Price per share.
            direction: 1 for buy, -1 for sell.
            is_open: True if opening position, False if closing.

        Returns:
            Total commission in account currency.
        """
        ...

    @abstractmethod
    def apply_slippage(self, price: float, direction: int) -> float:
        """Apply slippage to execution price.

        Args:
            price: Base price.
            direction: 1 for buy (price goes up), -1 for sell (price goes down).

        Returns:
            Adjusted price after slippage.
        """
        ...


# ── GlobalEquityEngine (delegates to new module for market rules) ────────────

class GlobalEquityEngine(BaseEngine):
    """US / HK equity engine, selected by market parameter.

    Market rules:
      US:
        - T+0, long/short allowed
        - Zero commission (retail brokers)
        - Fractional shares supported (round to 0.01)
        - Low slippage (high liquidity)
      HK:
        - T+0, long/short allowed
        - Stamp tax 0.13% bilateral + levies
        - Lot-size rounding (varies per stock, default 100)
        - Higher slippage than US

    Config keys:
      - slippage_us: default 0.0005
      - slippage_hk: default 0.001
      - hk_stamp_tax: default 0.0013 (0.13% bilateral)
      - hk_commission: default 0.00015 (万1.5)
      - hk_levy: default 0.0000565 (SFC + FRC)
      - hk_settlement: default 0.00002 (CCASS)
    """

    def __init__(self, config: dict, market: str = "us"):
        super().__init__(config)
        self.market = market.lower()

        # US defaults
        self.slippage_us: float = config.get("slippage_us", 0.0005)
        # HK defaults
        self.slippage_hk: float = config.get("slippage_hk", 0.001)
        self.hk_stamp_tax: float = config.get("hk_stamp_tax", 0.0013)
        self.hk_commission: float = config.get("hk_commission", 0.00015)
        self.hk_levy: float = config.get("hk_levy", 0.0000565)
        self.hk_settlement: float = config.get("hk_settlement", 0.00002)

    def can_execute(self, symbol: str, direction: int, price: float) -> bool:
        """US/HK: T+0, both directions allowed."""
        return True

    def round_size(self, raw_size: float, price: float) -> float:
        """US: fractional shares (0.01). HK: 100-share lots (simplified)."""
        if self.market == "hk":
            return max(int(raw_size / 100) * 100, 0)
        return round(max(raw_size, 0.0), 2)

    def calc_commission(self, size: float, price: float, direction: int, is_open: bool) -> float:
        """US: zero commission. HK: stamp tax + levies."""
        if self.market == "hk":
            notional = size * price
            comm = notional * self.hk_commission       # broker commission
            comm += notional * self.hk_stamp_tax       # stamp tax bilateral
            comm += notional * self.hk_levy            # SFC + FRC levies
            comm += notional * self.hk_settlement      # CCASS settlement
            return comm
        # US: zero commission (SEC fee negligible)
        return 0.0

    def apply_slippage(self, price: float, direction: int) -> float:
        """US: low slippage. HK: moderate slippage."""
        rate = self.slippage_hk if self.market == "hk" else self.slippage_us
        return price * (1 + direction * rate)


class CompositeEngine(BaseEngine):
    """Composite engine that routes to market-specific engines.

    Automatically selects the right engine based on symbol prefix.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self._engines: dict[str, GlobalEquityEngine] = {
            "hk": GlobalEquityEngine(config, market="hk"),
            "us": GlobalEquityEngine(config, market="us"),
        }

    def _get_engine(self, symbol: str) -> GlobalEquityEngine:
        """Get the appropriate engine for a symbol."""
        symbol = symbol.upper()
        if symbol.startswith("HK."):
            return self._engines["hk"]
        elif symbol.startswith("US."):
            return self._engines["us"]
        else:
            # Default to US for ambiguous symbols
            return self._engines["us"]

    def can_execute(self, symbol: str, direction: int, price: float) -> bool:
        """Delegate to market-specific engine."""
        return self._get_engine(symbol).can_execute(symbol, direction, price)

    def round_size(self, raw_size: float, price: float, symbol: str = "") -> float:
        """Delegate to market-specific engine."""
        return self._get_engine(symbol).round_size(raw_size, price)

    def calc_commission(self, size: float, price: float, direction: int, is_open: bool, symbol: str = "") -> float:
        """Delegate to market-specific engine."""
        return self._get_engine(symbol).calc_commission(size, price, direction, is_open)

    def apply_slippage(self, price: float, direction: int, symbol: str = "") -> float:
        """Delegate to market-specific engine."""
        return self._get_engine(symbol).apply_slippage(price, direction)


# ── Convenience functions ────────────────────────────────────────────────────

def _detect_market(symbol: str) -> str:
    """Auto-detect market from symbol prefix.

    Args:
        symbol: Stock symbol (e.g. 'HK.00700', 'US.AAPL', 'AAPL').

    Returns:
        'hk' or 'us'.
    """
    symbol = symbol.upper().strip()
    if symbol.startswith("HK."):
        return "hk"
    return "us"  # default to US


def create_engine(market: str, config: Optional[dict] = None) -> BaseEngine:
    """Create a backtest engine for a specific market.

    Args:
        market: "HK", "US", or "composite" for auto-routing.
        config: Optional configuration dict.

    Returns:
        BaseEngine instance.
    """
    config = config or {}
    market = market.lower()

    if market == "composite":
        return CompositeEngine(config)
    elif market in ("hk", "us"):
        return GlobalEquityEngine(config, market=market)
    else:
        raise ValueError(f"Unsupported market: {market}. Use 'HK', 'US', or 'composite'.")


def get_commission_rate(market: str) -> dict[str, float]:
    """Get commission rates for a market.

    Args:
        market: "HK" or "US".

    Returns:
        Dict with commission components.
    """
    engine = GlobalEquityEngine({}, market=market.lower())
    if market.lower() == "hk":
        return {
            "commission": engine.hk_commission,
            "stamp_tax": engine.hk_stamp_tax,
            "levy": engine.hk_levy,
            "settlement": engine.hk_settlement,
            "slippage": engine.slippage_hk,
            "total_pct": engine.hk_commission + engine.hk_stamp_tax + engine.hk_levy + engine.hk_settlement,
        }
    else:
        return {
            "commission": 0.0,
            "stamp_tax": 0.0,
            "levy": 0.0,
            "settlement": 0.0,
            "slippage": engine.slippage_us,
            "total_pct": 0.0,
        }
