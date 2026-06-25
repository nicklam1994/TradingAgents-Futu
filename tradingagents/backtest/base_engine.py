"""Base backtest engine with shared bar-by-bar execution loop.

Ported from Vibe-Trading agent/backtest/engines/base.py (767 lines).
Stripped Vibe-specific loaders (rsshub_events, tushare_fundamentals) —
those are plugin concerns, not core engine logic.

All market engines inherit from BaseEngine and override market-rule methods.
The shared run() handles: data loading → signal generation →
pre-compute target weights → bar-by-bar execution with market rule
enforcement → metrics → trade records.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from tradingagents.backtest.models import EquitySnapshot, Position, TradeRecord

logger = logging.getLogger(__name__)


# ─── Signal alignment ────────────────────────────────────────────────────────

def align_signals(
    data_map: Dict[str, pd.DataFrame],
    signal_map: Dict[str, pd.Series],
    codes: List[str],
    optimizer: Optional[Callable] = None,
) -> tuple:
    """Build aligned date index, close matrix, target-position matrix, return matrix.

    Signal is shifted by 1 bar (next-bar-open semantics) then normalised so
    ``sum(abs(weights)) <= 1.0``.

    Args:
        data_map: code -> OHLCV DataFrame.
        signal_map: code -> signal Series.
        codes: Valid instrument codes.
        optimizer: Optional weight optimiser ``(ret, pos, dates) -> pos``.

    Returns:
        (dates, close_df, positions_df, returns_df)
    """
    all_dates: set = set()
    for c in codes:
        all_dates.update(data_map[c].index)
    dates = pd.DatetimeIndex(sorted(all_dates))

    close = pd.DataFrame(index=dates, columns=codes, dtype=float)
    for c in codes:
        close[c] = data_map[c]["close"].reindex(dates)

    # ffill with limit to avoid masking long suspensions (e.g. 3-week halt)
    # Cross-market needs larger limit (Chinese New Year can be 9-10 bars)
    ffill_limit = 10 if len(codes) > 1 else 5
    close = close.ffill(limit=ffill_limit)

    # Drop symbols that are entirely NaN (no data overlap with date range)
    all_nan_cols = [c for c in codes if close[c].isna().all()]
    if all_nan_cols:
        logger.warning("Symbols dropped (no usable price data): %s", all_nan_cols)
        codes = [c for c in codes if c not in all_nan_cols]
        if not codes:
            raise ValueError("All symbols have no data in the requested date range")
        close = close[codes]

    pos = pd.DataFrame(0.0, index=dates, columns=codes)
    for c in codes:
        # Shift on each symbol's OWN trading calendar, then ffill to unified
        own_dates = data_map[c].index
        raw = signal_map[c].reindex(own_dates).fillna(0.0).clip(-1.0, 1.0)
        shifted = raw.shift(1).fillna(0.0)
        pos[c] = shifted.reindex(dates).ffill(limit=ffill_limit).fillna(0.0)

    ret = close.pct_change().fillna(0.0)

    if optimizer is not None:
        pos = optimizer(ret, pos, dates)

    scale = pos.abs().sum(axis=1).clip(lower=1.0)
    pos = pos.div(scale, axis=0)

    return dates, close, pos, ret


# ─── Base Engine ─────────────────────────────────────────────────────────────

class BaseEngine(ABC):
    """Abstract base for all market engines.

    Subclasses override market-rule methods:
      - can_execute: whether a trade is allowed by market rules
      - round_size: lot-size rounding
      - calc_commission: fee structure
      - apply_slippage: slippage model
      - on_bar: per-bar hooks (funding fees, liquidation, etc.)

    The 5-step pipeline exposed via ``run()``:
      1. load_data()  — fetch OHLCV via loader
      2. generate_signals() — produce signal map via signal engine
      3. execute_trades()  — bar-by-bar execution with market rules
      4. calculate_metrics() — Sharpe, Sortino, Drawdown, WinRate, ProfitFactor
      5. run() — orchestrates 1-4 and returns metrics dict
    """

    def __init__(self, config: dict):
        self.config = config
        self.initial_capital: float = config.get("initial_cash", 1_000_000)
        self.default_leverage: float = config.get("leverage", 1.0)
        self.capital: float = self.initial_capital
        self.positions: Dict[str, Position] = {}
        self.trades: List[TradeRecord] = []
        self.equity_snapshots: List[EquitySnapshot] = []
        self._bar_idx: int = 0
        # Set by _rebalance/_close_position for subclass use (e.g. lot lookup)
        self._active_symbol: str = ""

    # ── Market rule interface (subclass must implement) ──────────────────────

    @abstractmethod
    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:
        """Whether market rules allow this trade.

        Args:
            symbol: Instrument identifier.
            direction: 1 (long), -1 (short), 0 (close).
            bar: Current bar data (OHLCV + extras).

        Returns:
            True if allowed.
        """

    @abstractmethod
    def round_size(self, raw_size: float, price: float) -> float:
        """Round position size per market lot rules.

        Args:
            raw_size: Desired size.
            price: Current price.

        Returns:
            Rounded size.
        """

    @abstractmethod
    def calc_commission(
        self, size: float, price: float, direction: int, is_open: bool
    ) -> float:
        """Calculate commission for a trade.

        Args:
            size: Trade size.
            price: Execution price.
            direction: 1 or -1.
            is_open: True for opening, False for closing.

        Returns:
            Commission amount.
        """

    @abstractmethod
    def apply_slippage(self, price: float, direction: int) -> float:
        """Apply slippage to execution price.

        Args:
            price: Raw price.
            direction: 1 (buying / covering short) or -1 (selling / shorting).

        Returns:
            Slipped price.
        """

    def on_bar(
        self, symbol: str, bar: pd.Series, timestamp: pd.Timestamp
    ) -> None:
        """Per-bar market-rule hook (funding fees, liquidation, etc.).

        Default: no-op. Override in subclass as needed.
        """

    # ── PnL / margin calculation hooks ──────────────────────────────────────
    # Override in futures engines to inject contract multiplier.

    def _calc_pnl(
        self,
        symbol: str,
        direction: int,
        size: float,
        entry_price: float,
        exit_price: float,
    ) -> float:
        """Realised PnL for a closed position."""
        return direction * size * (exit_price - entry_price)

    def _calc_margin(
        self, symbol: str, size: float, price: float, leverage: float
    ) -> float:
        """Margin (collateral) required for a position."""
        return size * price / leverage

    def _calc_raw_size(
        self, symbol: str, target_notional: float, price: float
    ) -> float:
        """Convert target notional exposure to number of units/contracts."""
        return target_notional / price

    # ── 5-step pipeline ─────────────────────────────────────────────────────

    def run(
        self,
        config: Dict[str, Any],
        data_map: Dict[str, pd.DataFrame],
        signal_map: Dict[str, pd.Series],
        bars_per_year: int = 252,
    ) -> Dict[str, Any]:
        """Full backtest pipeline: align → execute → metrics.

        Args:
            config: Backtest configuration dict.
            data_map: code -> OHLCV DataFrame (pre-loaded).
            signal_map: code -> signal Series (pre-generated).
            bars_per_year: Annualisation factor (252 for equity).

        Returns:
            Metrics dictionary.
        """
        codes = sorted(c for c in signal_map if c in data_map)
        if not codes:
            raise ValueError("No valid symbols overlap between data_map and signal_map")

        # Step 1-2: Align signals (load_data + generate_signals are caller's job)
        dates, close_df, target_pos, ret_df = align_signals(
            data_map, signal_map, codes
        )

        # Sync codes after align may have dropped all-NaN symbols
        codes = [c for c in codes if c in target_pos.columns]

        # Step 3: Execute trades
        self._execute_bars(dates, data_map, close_df, target_pos, codes)

        # Step 4: Calculate metrics
        equity_series = pd.Series(
            [s.equity for s in self.equity_snapshots],
            index=[s.timestamp for s in self.equity_snapshots],
        )
        bench_ret = (
            ret_df.mean(axis=1)
            if ret_df.shape[1] > 0
            else pd.Series(0.0, index=dates)
        )

        from tradingagents.backtest.metrics import calc_metrics

        m = calc_metrics(
            equity_series, self.trades, self.initial_capital, bars_per_year, bench_ret
        )

        return m

    # ── Execution loop ──────────────────────────────────────────────────────

    def _execute_bars(
        self,
        dates: pd.DatetimeIndex,
        data_map: Dict[str, pd.DataFrame],
        close_df: pd.DataFrame,
        target_pos: pd.DataFrame,
        codes: List[str],
    ) -> None:
        """Bar-by-bar execution with market rule enforcement."""
        for i, ts in enumerate(dates):
            self._bar_idx = i

            # a. Per-bar hooks (funding fees, liquidation checks)
            for c in codes:
                if ts in data_map[c].index:
                    self.on_bar(c, data_map[c].loc[ts], ts)

            # b. Rebalance each symbol to target weight
            equity = self._calc_equity(close_df, ts)
            for c in codes:
                try:
                    target_w = (
                        float(target_pos.at[ts, c])
                        if ts in target_pos.index
                        else 0.0
                    )
                    self._rebalance(
                        c, target_w, data_map.get(c), ts, equity
                    )
                except Exception as exc:
                    logger.warning(
                        "Rebalance failed for %s at %s: %s", c, ts, exc
                    )

            # c. Record equity snapshot
            snap_equity = self._calc_equity(close_df, ts)
            total_unrealized = 0.0
            for p in self.positions.values():
                cp = self._safe_price(close_df, ts, p.symbol, p.entry_price)
                total_unrealized += self._calc_pnl(
                    p.symbol, p.direction, p.size, p.entry_price, cp
                )
            self.equity_snapshots.append(
                EquitySnapshot(
                    timestamp=ts,
                    capital=self.capital,
                    unrealized=total_unrealized,
                    equity=snap_equity,
                    positions=len(self.positions),
                )
            )

        # d. Force close all remaining positions
        if len(dates) > 0:
            last_ts = dates[-1]
            for c in list(self.positions.keys()):
                price = self._safe_price(
                    close_df, last_ts, c, self.positions[c].entry_price
                )
                self._close_position(c, price, last_ts, "end_of_backtest")

    def _calc_equity(
        self, close_df: pd.DataFrame, ts: pd.Timestamp
    ) -> float:
        """Total equity = free cash + sum(margin + unrealised) per position."""
        equity = self.capital
        for sym, pos in self.positions.items():
            cp = self._safe_price(close_df, ts, sym, pos.entry_price)
            margin = self._calc_margin(sym, pos.size, pos.entry_price, pos.leverage)
            unrealized = self._calc_pnl(
                sym, pos.direction, pos.size, pos.entry_price, cp
            )
            equity += margin + unrealized
        return equity

    def _rebalance(
        self,
        symbol: str,
        target_weight: float,
        df: Optional[pd.DataFrame],
        ts: pd.Timestamp,
        equity: float,
    ) -> None:
        """Adjust position for *symbol* toward *target_weight*."""
        self._active_symbol = symbol
        target_dir = (
            1 if target_weight > 1e-9 else (-1 if target_weight < -1e-9 else 0)
        )
        current_pos = self.positions.get(symbol)

        # Nothing to do
        if current_pos is None and target_dir == 0:
            return
        if df is None or ts not in df.index:
            return

        bar = df.loc[ts]

        # Close if target is flat or direction changed
        if current_pos is not None:
            need_close = target_dir == 0 or target_dir != current_pos.direction
            if need_close:
                if self.can_execute(symbol, 0, bar):
                    open_price = float(bar.get("open", bar.get("close", 0)))
                    price = self.apply_slippage(open_price, -current_pos.direction)
                    self._close_position(symbol, price, ts, "signal")
                else:
                    return  # blocked (e.g. limit-down can't sell)

        # Open new if target non-zero and no remaining position
        if target_dir != 0 and symbol not in self.positions:
            if not self.can_execute(symbol, target_dir, bar):
                return  # blocked (e.g. A-share no-short)

            open_price = float(bar.get("open", bar.get("close", 0)))
            if open_price <= 0:
                return

            slipped = self.apply_slippage(open_price, target_dir)
            leverage = self.default_leverage
            target_notional = abs(target_weight) * equity * leverage
            raw_size = self._calc_raw_size(symbol, target_notional, slipped)
            size = self.round_size(raw_size, slipped)
            if size <= 0:
                return

            margin = self._calc_margin(symbol, size, slipped, leverage)
            comm = self.calc_commission(size, slipped, target_dir, is_open=True)

            # Capital check — reduce if insufficient
            if margin + comm > self.capital:
                available = self.capital - comm
                if available <= 0:
                    return
                size = self.round_size(
                    self._calc_raw_size(symbol, available * leverage, slipped),
                    slipped,
                )
                if size <= 0:
                    return
                margin = self._calc_margin(symbol, size, slipped, leverage)
                comm = self.calc_commission(size, slipped, target_dir, is_open=True)

            self.capital -= margin + comm
            self.positions[symbol] = Position(
                symbol=symbol,
                direction=target_dir,
                entry_price=slipped,
                entry_time=ts,
                size=size,
                leverage=leverage,
                entry_bar_idx=self._bar_idx,
                entry_commission=comm,
            )

    def _close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_time: pd.Timestamp,
        reason: str,
    ) -> None:
        """Close position, record trade, return capital."""
        self._active_symbol = symbol
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return

        pnl = self._calc_pnl(
            symbol, pos.direction, pos.size, pos.entry_price, exit_price
        )
        margin = self._calc_margin(
            symbol, pos.size, pos.entry_price, pos.leverage
        )
        pnl_pct = pnl / margin * 100 if margin > 1e-9 else 0.0
        exit_comm = self.calc_commission(
            pos.size, exit_price, pos.direction, is_open=False
        )

        self.capital += margin + pnl - exit_comm

        holding_bars = max(self._bar_idx - pos.entry_bar_idx, 0)

        self.trades.append(
            TradeRecord(
                symbol=symbol,
                direction=pos.direction,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                entry_time=pos.entry_time,
                exit_time=exit_time,
                size=pos.size,
                leverage=pos.leverage,
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason=reason,
                holding_bars=holding_bars,
                commission=pos.entry_commission + exit_comm,
            )
        )

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_price(
        close_df: pd.DataFrame,
        ts: pd.Timestamp,
        symbol: str,
        fallback: float,
    ) -> float:
        """Get close price with fallback."""
        if ts in close_df.index and symbol in close_df.columns:
            val = close_df.at[ts, symbol]
            if pd.notna(val):
                return float(val)
        return fallback
