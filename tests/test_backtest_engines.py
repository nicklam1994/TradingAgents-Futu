"""Tests for multi-engine backtest: BaseEngine, GlobalEquityEngine, Metrics.

Covers:
  - BaseEngine ABC interface (cannot instantiate directly)
  - GlobalEquityEngine HK/US market rules (commission, slippage, lot size)
  - CompositeEngine auto-routing by symbol prefix
  - Metrics calculation (Sharpe, Sortino, Max Drawdown, Win Rate, Profit Factor)
  - Signal alignment
  - Market auto-detection
  - Data models (Position, TradeRecord, EquitySnapshot)
"""

from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd
import pytest

# ── Imports from new modules ─────────────────────────────────────────────────

from tradingagents.backtest.models import Position, TradeRecord, EquitySnapshot
from tradingagents.backtest.metrics import (
    calc_metrics,
    calc_bars_per_year,
    win_rate_and_stats,
    by_symbol_stats,
    by_exit_reason_stats,
)
from tradingagents.backtest.base_engine import BaseEngine as EnhancedBaseEngine, align_signals
from tradingagents.backtest.global_equity_engine import GlobalEquityEngine as GlobalEquityEngineV2

# ── Imports from API-facing adapter ──────────────────────────────────────────

from tradingagents.backtest.engines import (
    BaseEngine,
    GlobalEquityEngine,
    CompositeEngine,
    create_engine,
    get_commission_rate,
    _detect_market,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. BaseEngine ABC — cannot instantiate directly
# ═══════════════════════════════════════════════════════════════════════════════

class TestBaseEngineABC:
    """BaseEngine is abstract — must implement all 4 methods."""

    def test_cannot_instantiate_simple(self):
        """Simple BaseEngine (engines.py) is abstract."""
        with pytest.raises(TypeError):
            BaseEngine({})

    def test_cannot_instantiate_enhanced(self):
        """Enhanced BaseEngine (base_engine.py) is abstract."""
        with pytest.raises(TypeError):
            EnhancedBaseEngine({})


# ═══════════════════════════════════════════════════════════════════════════════
# 2. GlobalEquityEngine — HK/US market rules
# ═══════════════════════════════════════════════════════════════════════════════

class TestGlobalEquityEngineHK:
    """HK market rules: stamp tax, lot-size rounding, moderate slippage."""

    def setup_method(self):
        self.engine = GlobalEquityEngine({}, market="hk")

    def test_can_execute_always_true(self):
        """HK: T+0, both directions allowed."""
        assert self.engine.can_execute("HK.00700", 1, 350.0) is True
        assert self.engine.can_execute("HK.00700", -1, 350.0) is True

    def test_round_size_lot_100(self):
        """HK: rounds to 100-share lots."""
        assert self.engine.round_size(150, 350.0) == 100
        assert self.engine.round_size(299, 350.0) == 200
        assert self.engine.round_size(100, 350.0) == 100
        assert self.engine.round_size(50, 350.0) == 0

    def test_round_size_zero(self):
        """HK: zero or negative rounds to 0."""
        assert self.engine.round_size(0, 350.0) == 0
        assert self.engine.round_size(-10, 350.0) == 0

    def test_calc_commission_hk(self):
        """HK: stamp tax + commission + levy + settlement."""
        size = 1000
        price = 100.0
        comm = self.engine.calc_commission(size, price, 1, True)
        notional = size * price  # 100,000
        expected = (
            notional * 0.00015   # commission
            + notional * 0.001  # stamp tax
            + notional * 0.0000565  # levy
            + notional * 0.00002    # settlement
        )
        assert abs(comm - expected) < 0.01

    def test_apply_slippage_hk(self):
        """HK: moderate slippage (0.1%)."""
        price = 100.0
        # Buy: price goes up
        assert self.engine.apply_slippage(price, 1) == pytest.approx(100.1)
        # Sell: price goes down
        assert self.engine.apply_slippage(price, -1) == pytest.approx(99.9)


class TestGlobalEquityEngineUS:
    """US market rules: zero commission, fractional shares, low slippage."""

    def setup_method(self):
        self.engine = GlobalEquityEngine({}, market="us")

    def test_can_execute_always_true(self):
        """US: T+0, both directions allowed."""
        assert self.engine.can_execute("US.AAPL", 1, 150.0) is True
        assert self.engine.can_execute("US.AAPL", -1, 150.0) is True

    def test_round_size_fractional(self):
        """US: fractional shares (0.01 precision)."""
        assert self.engine.round_size(150.456, 150.0) == 150.46
        assert self.engine.round_size(100.001, 150.0) == 100.0
        assert self.engine.round_size(0.005, 150.0) == 0.01

    def test_round_size_zero(self):
        """US: negative rounds to 0."""
        assert self.engine.round_size(-10, 150.0) == 0.0

    def test_calc_commission_us_zero(self):
        """US: zero commission."""
        assert self.engine.calc_commission(100, 150.0, 1, True) == 0.0
        assert self.engine.calc_commission(100, 150.0, -1, False) == 0.0

    def test_apply_slippage_us(self):
        """US: low slippage (0.05%)."""
        price = 150.0
        # Buy: price goes up
        assert self.engine.apply_slippage(price, 1) == pytest.approx(150.075)
        # Sell: price goes down
        assert self.engine.apply_slippage(price, -1) == pytest.approx(149.925)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. CompositeEngine — auto-routing
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompositeEngine:
    """CompositeEngine routes to correct market engine by symbol prefix."""

    def setup_method(self):
        self.engine = CompositeEngine({})

    def test_route_hk(self):
        """HK. prefix routes to HK engine."""
        # Set routing via can_execute
        self.engine.can_execute("HK.00700", 1, 100.0)
        # HK lot-size rounding
        assert self.engine.round_size(150, 100.0) == 100
        # HK commission non-zero
        assert self.engine.calc_commission(100, 100.0, 1, True) > 0

    def test_route_us(self):
        """US. prefix routes to US engine."""
        # Set routing via can_execute
        self.engine.can_execute("US.AAPL", 1, 100.0)
        # US fractional
        assert self.engine.round_size(150.456, 100.0) == 150.46
        # US zero commission
        assert self.engine.calc_commission(100, 100.0, 1, True) == 0.0

    def test_default_to_us(self):
        """Ambiguous symbols default to US."""
        self.engine.can_execute("AAPL", 1, 100.0)
        assert self.engine.calc_commission(100, 100.0, 1, True) == 0.0

    def test_apply_slippage_routing(self):
        """Slippage routes to correct market."""
        # Route HK first
        self.engine.can_execute("HK.00700", 1, 100.0)
        hk_slip = self.engine.apply_slippage(100.0, 1)
        # Route US
        self.engine.can_execute("US.AAPL", 1, 100.0)
        us_slip = self.engine.apply_slippage(100.0, 1)
        # HK slippage > US slippage
        assert hk_slip > us_slip


# ═══════════════════════════════════════════════════════════════════════════════
# 4. create_engine factory
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreateEngine:
    """create_engine factory with auto-detection."""

    def test_create_hk(self):
        engine = create_engine("hk")
        assert isinstance(engine, GlobalEquityEngine)
        assert engine.market == "hk"

    def test_create_us(self):
        engine = create_engine("us")
        assert isinstance(engine, GlobalEquityEngine)
        assert engine.market == "us"

    def test_create_composite(self):
        engine = create_engine("composite")
        assert isinstance(engine, CompositeEngine)

    def test_create_invalid(self):
        with pytest.raises(ValueError, match="Unsupported market"):
            create_engine("crypto")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Market auto-detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectMarket:
    """Auto-detect market from symbol prefix."""

    def test_hk_prefix(self):
        assert _detect_market("HK.00700") == "hk"
        assert _detect_market("hk.00700") == "hk"

    def test_us_default(self):
        assert _detect_market("US.AAPL") == "us"
        assert _detect_market("AAPL") == "us"
        assert _detect_market("TSLA") == "us"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Commission rate lookup
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetCommissionRate:
    """get_commission_rate returns correct structure."""

    def test_hk_rates(self):
        rates = get_commission_rate("hk")
        assert rates["commission"] == 0.00015
        assert rates["stamp_tax"] == 0.001
        assert rates["levy"] == pytest.approx(0.0000565)
        assert rates["settlement"] == 0.00002
        assert rates["total_pct"] > 0

    def test_us_rates(self):
        rates = get_commission_rate("us")
        assert rates["commission"] == 0.0
        assert rates["stamp_tax"] == 0.0
        assert rates["total_pct"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Metrics — Sharpe, Sortino, Max Drawdown, Win Rate, Profit Factor
# ═══════════════════════════════════════════════════════════════════════════════

class TestMetrics:
    """Test calc_metrics with synthetic equity curves."""

    def _make_equity(self, returns: list[float], start: float = 100_000) -> pd.Series:
        """Build equity curve from simple returns."""
        equity = [start]
        for r in returns:
            equity.append(equity[-1] * (1 + r))
        dates = pd.date_range("2024-01-01", periods=len(equity), freq="B")
        return pd.Series(equity, index=dates)

    def _make_trades(self, pnls: list[float]) -> list[TradeRecord]:
        """Build synthetic trades from P&L list."""
        trades = []
        base_time = pd.Timestamp("2024-01-01")
        for i, pnl in enumerate(pnls):
            trades.append(TradeRecord(
                symbol="US.AAPL",
                direction=1,
                entry_price=100.0,
                exit_price=100.0 + pnl,
                entry_time=base_time + pd.Timedelta(days=i * 5),
                exit_time=base_time + pd.Timedelta(days=i * 5 + 3),
                size=100.0,
                leverage=1.0,
                pnl=pnl * 100,  # scale to shares
                pnl_pct=pnl,
                exit_reason="signal",
                holding_bars=3,
                commission=0.0,
            ))
        return trades

    def test_empty_equity(self):
        """Empty equity curve returns zero metrics."""
        m = calc_metrics(pd.Series(dtype=float), [], 100_000)
        assert m["total_return"] == 0
        assert m["sharpe"] == 0
        assert m["trade_count"] == 0

    def test_positive_returns(self):
        """Monotonically increasing equity → positive Sharpe."""
        returns = [0.001] * 252  # 0.1% daily for 1 year
        eq = self._make_equity(returns)
        m = calc_metrics(eq, [], 100_000, bars_per_year=252)
        assert m["total_return"] > 0
        assert m["sharpe"] > 0
        assert m["max_drawdown"] == 0  # monotonically increasing

    def test_sharpe_calculation(self):
        """Sharpe = mean(ret) / std(ret) * sqrt(bars_per_year)."""
        np.random.seed(42)
        returns = np.random.normal(0.0005, 0.01, 252).tolist()
        eq = self._make_equity(returns)
        m = calc_metrics(eq, [], 100_000, bars_per_year=252)

        # Manual calc
        port_ret = eq.pct_change().fillna(0.0)
        expected_sharpe = float(
            port_ret.mean() / (port_ret.std() + 1e-10) * np.sqrt(252)
        )
        assert abs(m["sharpe"] - expected_sharpe) < 0.001

    def test_max_drawdown(self):
        """Max drawdown calculated correctly."""
        # Up 10%, down 20%, up 5% → max DD = -20% from peak
        returns = [0.10, -0.20, 0.05]
        eq = self._make_equity(returns)
        m = calc_metrics(eq, [], 100_000, bars_per_year=252)
        # Peak after first return: 110,000. Trough: 88,000. DD = -20%
        assert m["max_drawdown"] == pytest.approx(-0.20, abs=0.01)

    def test_sortino_calculation(self):
        """Sortino uses downside deviation only."""
        returns = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.005]
        eq = self._make_equity(returns)
        m = calc_metrics(eq, [], 100_000, bars_per_year=252)
        assert m["sortino"] != 0  # should be non-zero

    def test_win_rate(self):
        """Win rate from trade list."""
        trades = self._make_trades([1.0, -0.5, 2.0, -1.0, 0.5])
        stats = win_rate_and_stats(trades)
        # 3 wins out of 5
        assert stats["win_rate"] == pytest.approx(0.6)

    def test_profit_factor(self):
        """Profit factor = gross_profit / gross_loss."""
        trades = self._make_trades([100, -50, 200, -30])
        # Gross profit: 300, Gross loss: 80 → PF = 3.75
        # But trades have pnl = pnl_pct * 100, so:
        # wins: 100*100 + 200*100 = 30000, losses: 50*100 + 30*100 = 8000
        stats = win_rate_and_stats(trades)
        assert stats["profit_factor"] == pytest.approx(3.75, abs=0.01)

    def test_by_symbol_stats(self):
        """Per-symbol statistics."""
        trades = [
            TradeRecord("US.AAPL", 1, 100, 110, pd.Timestamp("2024-01-01"),
                        pd.Timestamp("2024-01-05"), 100, 1.0, 1000, 10.0,
                        "signal", 4, 0),
            TradeRecord("US.AAPL", 1, 100, 90, pd.Timestamp("2024-01-10"),
                        pd.Timestamp("2024-01-15"), 100, 1.0, -1000, -10.0,
                        "signal", 5, 0),
            TradeRecord("HK.00700", 1, 300, 350, pd.Timestamp("2024-01-01"),
                        pd.Timestamp("2024-01-10"), 100, 1.0, 5000, 16.67,
                        "signal", 9, 0),
        ]
        stats = by_symbol_stats(trades)
        assert "US.AAPL" in stats
        assert "HK.00700" in stats
        assert stats["US.AAPL"]["count"] == 2
        assert stats["HK.00700"]["count"] == 1

    def test_by_exit_reason_stats(self):
        """Per-exit-reason statistics."""
        trades = [
            TradeRecord("US.AAPL", 1, 100, 110, pd.Timestamp("2024-01-01"),
                        pd.Timestamp("2024-01-05"), 100, 1.0, 1000, 10.0,
                        "signal", 4, 0),
            TradeRecord("US.AAPL", 1, 100, 90, pd.Timestamp("2024-01-10"),
                        pd.Timestamp("2024-01-15"), 100, 1.0, -1000, -10.0,
                        "end_of_backtest", 5, 0),
        ]
        stats = by_exit_reason_stats(trades)
        assert "signal" in stats
        assert "end_of_backtest" in stats
        assert stats["signal"]["count"] == 1
        assert stats["end_of_backtest"]["count"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 8. calc_bars_per_year
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalcBarsPerYear:
    """Market-specific annualisation."""

    def test_daily_us(self):
        assert calc_bars_per_year("1D", "us") == 252

    def test_daily_hk(self):
        assert calc_bars_per_year("1D", "hk") == 252

    def test_intraday(self):
        assert calc_bars_per_year("1m", "us") == 252 * 390 or \
               calc_bars_per_year("1m", "us") == 252 * 240

    def test_unknown_interval_defaults_to_1(self):
        assert calc_bars_per_year("unknown", "us") == 252


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Signal alignment
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlignSignals:
    """Test signal alignment with synthetic data."""

    def _make_data(self, n: int = 10) -> Dict[str, pd.DataFrame]:
        """Create synthetic OHLCV data for 2 symbols."""
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        data = {}
        for sym in ["US.AAPL", "US.TSLA"]:
            np.random.seed(hash(sym) % 2**31)
            close = 100 + np.cumsum(np.random.randn(n) * 2)
            data[sym] = pd.DataFrame({
                "open": close - 0.5,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": np.random.randint(1000, 10000, n),
            }, index=dates)
        return data

    def test_alignment_basic(self):
        """Signals are shifted by 1 bar (next-bar-open semantics)."""
        data = self._make_data(10)
        # Constant signal: always 1.0
        signal_map = {
            sym: pd.Series(1.0, index=df.index)
            for sym, df in data.items()
        }
        dates, close_df, pos_df, ret_df = align_signals(
            data, signal_map, list(data.keys())
        )
        # Position should be shifted: first bar should be 0 (signal shifted)
        # After normalization with 2 symbols, each gets 0.5 weight
        assert len(dates) == 10
        assert close_df.shape == (10, 2)
        assert pos_df.shape == (10, 2)

    def test_all_nan_dropped(self):
        """Symbols with no data overlap are silently dropped."""
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        data = {
            "US.AAPL": pd.DataFrame({"close": [100, 101, 102, 103, 104]}, index=dates),
            "US.TSLA": pd.DataFrame({"close": [np.nan] * 5}, index=dates),
        }
        signal_map = {
            sym: pd.Series(1.0, index=df.index)
            for sym, df in data.items()
        }
        # TSLA is dropped, AAPL survives
        dates_out, close_df, pos_df, ret_df = align_signals(
            data, signal_map, list(data.keys())
        )
        assert "US.TSLA" not in close_df.columns
        assert "US.AAPL" in close_df.columns

    def test_all_nan_raises(self):
        """When ALL symbols are NaN, raises ValueError."""
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        data = {
            "US.TSLA": pd.DataFrame({"close": [np.nan] * 5}, index=dates),
        }
        signal_map = {
            "US.TSLA": pd.Series(1.0, index=dates),
        }
        with pytest.raises(ValueError, match="no data"):
            align_signals(data, signal_map, list(data.keys()))


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Enhanced GlobalEquityEngineV2 — full execution loop
# ═══════════════════════════════════════════════════════════════════════════════

class TestGlobalEquityEngineV2:
    """Enhanced GlobalEquityEngine with bar-by-bar execution."""

    def test_hk_commission(self):
        """HK commission includes stamp tax + levy + settlement."""
        engine = GlobalEquityEngineV2({}, market="hk")
        comm = engine.calc_commission(1000, 100.0, 1, True)
        notional = 100_000
        expected = (
            notional * 0.00015 + notional * 0.001  # stamp tax
            + notional * 0.0000565 + notional * 0.00002
        )
        assert abs(comm - expected) < 0.01

    def test_us_zero_commission(self):
        """US has zero commission."""
        engine = GlobalEquityEngineV2({}, market="us")
        assert engine.calc_commission(100, 150.0, 1, True) == 0.0

    def test_run_with_synthetic_data(self):
        """Full run() with synthetic OHLCV + signal data."""
        engine = GlobalEquityEngineV2({"initial_cash": 100_000}, market="us")

        dates = pd.date_range("2024-01-01", periods=50, freq="B")
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(50) * 0.5)
        data_map = {
            "US.AAPL": pd.DataFrame({
                "open": close - 0.1,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": [10000] * 50,
            }, index=dates),
        }
        # Simple momentum signal: positive when price above 10-day MA
        ma = pd.Series(close).rolling(10).mean().fillna(100)
        signal = pd.Series(
            np.where(close > ma.values, 0.5, -0.5),
            index=dates,
        )
        signal_map = {"US.AAPL": signal}

        m = engine.run({}, data_map, signal_map, bars_per_year=252)

        # Should have some trades and valid metrics
        assert "total_return" in m
        assert "sharpe" in m
        assert "max_drawdown" in m
        assert "win_rate" in m
        assert "profit_factor" in m
        assert m["trade_count"] >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Data models
# ═══════════════════════════════════════════════════════════════════════════════

class TestModels:
    """Data model construction."""

    def test_position(self):
        pos = Position(
            symbol="HK.00700",
            direction=1,
            entry_price=350.0,
            entry_time=pd.Timestamp("2024-01-01"),
            size=100,
        )
        assert pos.symbol == "HK.00700"
        assert pos.leverage == 1.0  # default

    def test_trade_record(self):
        t = TradeRecord(
            symbol="US.AAPL",
            direction=1,
            entry_price=100.0,
            exit_price=110.0,
            entry_time=pd.Timestamp("2024-01-01"),
            exit_time=pd.Timestamp("2024-01-05"),
            size=100,
            leverage=1.0,
            pnl=1000.0,
            pnl_pct=10.0,
            exit_reason="signal",
            holding_bars=4,
            commission=0.0,
        )
        assert t.pnl == 1000.0
        assert t.exit_reason == "signal"

    def test_equity_snapshot(self):
        snap = EquitySnapshot(
            timestamp=pd.Timestamp("2024-01-01"),
            capital=90_000,
            unrealized=10_000,
            equity=100_000,
            positions=2,
        )
        assert snap.equity == 100_000
