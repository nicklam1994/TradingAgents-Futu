"""Backtest data models — shared across engines, metrics, and services.

Ported from Vibe-Trading backtest/models.py with TAF-specific adaptations.
These are pure data containers (dataclasses) with no business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class Position:
    """An open position held in the backtest portfolio.

    Attributes:
        symbol: Instrument identifier (e.g. 'HK.00700', 'US.AAPL').
        direction: 1 for long, -1 for short.
        entry_price: Slipped execution price at entry.
        entry_time: Timestamp of entry bar.
        size: Number of shares/contracts.
        leverage: Leverage multiplier (1.0 = no leverage).
        entry_bar_idx: Bar index at entry (for holding-period calc).
        entry_commission: Commission paid on entry.
    """

    symbol: str
    direction: int
    entry_price: float
    entry_time: pd.Timestamp
    size: float
    leverage: float = 1.0
    entry_bar_idx: int = 0
    entry_commission: float = 0.0


@dataclass
class TradeRecord:
    """A completed round-trip trade (entry + exit).

    Attributes:
        symbol: Instrument identifier.
        direction: 1 for long, -1 for short.
        entry_price: Entry execution price.
        exit_price: Exit execution price.
        entry_time: Entry timestamp.
        exit_time: Exit timestamp.
        size: Number of shares/contracts.
        leverage: Leverage used.
        pnl: Realised P&L in account currency.
        pnl_pct: P&L as percentage of margin deployed.
        exit_reason: Why the position was closed ('signal', 'end_of_backtest', etc.).
        holding_bars: Number of bars the position was held.
        commission: Total commission (entry + exit).
    """

    symbol: str
    direction: int
    entry_price: float
    exit_price: float
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    size: float
    leverage: float
    pnl: float
    pnl_pct: float
    exit_reason: str
    holding_bars: int
    commission: float


@dataclass
class EquitySnapshot:
    """Point-in-time portfolio state for equity curve construction.

    Attributes:
        timestamp: Bar timestamp.
        capital: Free cash.
        unrealized: Sum of unrealised P&L across open positions.
        equity: capital + unrealized + margin deployed.
        positions: Number of open positions.
    """

    timestamp: pd.Timestamp
    capital: float
    unrealized: float
    equity: float
    positions: int
