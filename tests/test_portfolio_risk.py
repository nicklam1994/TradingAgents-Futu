"""Tests for Portfolio Risk Service.

Tests all risk computation functions with known inputs and expected outputs.
No Futu connectivity required — pure computation tests.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from tradingagents.services.portfolio_risk_service import (
    PositionSnapshot,
    calculate_beta,
    calculate_concentration,
    calculate_cost_basis,
    calculate_drawdown,
    calculate_sharpe,
    calculate_var,
    generate_risk_report,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

def _make_position(
    symbol: str,
    qty: float = 100,
    cost_price: float = 10.0,
    average_cost: float = 10.0,
    market_val: float = 1200.0,
) -> PositionSnapshot:
    """Helper to create a PositionSnapshot."""
    return PositionSnapshot(
        symbol=symbol,
        stock_name=f"Test {symbol}",
        qty=qty,
        cost_price=cost_price,
        average_cost=average_cost,
        market_val=market_val,
        nominal_price=market_val / qty if qty > 0 else 0.0,
        pl_ratio=((market_val - qty * average_cost) / (qty * average_cost) * 100) if average_cost > 0 else 0,
        pl_val=market_val - qty * average_cost,
        unrealized_pl=market_val - qty * average_cost,
    )


@pytest.fixture
def three_positions():
    """Three positions with known weights: 50%, 30%, 20%."""
    return [
        _make_position("AAPL", qty=100, average_cost=100, market_val=5000),
        _make_position("NVDA", qty=50, average_cost=100, market_val=3000),
        _make_position("TSLA", qty=30, average_cost=100, market_val=2000),
    ]


@pytest.fixture
def price_history():
    """Simple uptrend price history (50 days)."""
    np.random.seed(42)
    base = np.linspace(100, 120, 50) + np.random.normal(0, 1, 50)
    return {
        "AAPL": base.tolist(),
        "NVDA": (base * 1.5).tolist(),
        "TSLA": (base * 0.8).tolist(),
    }


# ── Concentration tests ───────────────────────────────────────────────────

class TestConcentration:
    def test_empty_positions(self):
        result = calculate_concentration([])
        assert result.hhi == 0.0
        assert result.top_position_pct == 0.0

    def test_single_position(self):
        positions = [_make_position("AAPL", market_val=10000)]
        result = calculate_concentration(positions)
        # Single position = 100% weight → HHI = 100^2 = 10000
        assert result.hhi == pytest.approx(10000.0, abs=1)
        assert result.top_position_pct == pytest.approx(100.0, abs=0.01)

    def test_three_equal_positions(self):
        positions = [
            _make_position("A", market_val=3333.33),
            _make_position("B", market_val=3333.33),
            _make_position("C", market_val=3333.34),
        ]
        result = calculate_concentration(positions)
        # Equal weights ≈ 33.33% each → HHI ≈ 3 * (33.33)^2 ≈ 3333
        assert result.hhi < 4000  # Not concentrated
        assert result.hhi > 3000

    def test_concentrated_portfolio(self):
        positions = [
            _make_position("AAPL", market_val=9000),
            _make_position("NVDA", market_val=500),
            _make_position("TSLA", market_val=500),
        ]
        result = calculate_concentration(positions)
        # 90% in one position → very high HHI
        assert result.hhi > 8000
        assert result.top_position_pct == pytest.approx(90.0, abs=0.1)

    def test_top3_pct(self, three_positions):
        result = calculate_concentration(three_positions)
        assert result.top3_pct == pytest.approx(100.0, abs=0.1)

    def test_positions_sorted_by_weight(self, three_positions):
        result = calculate_concentration(three_positions)
        weights = [p["weight_pct"] for p in result.positions]
        assert weights == sorted(weights, reverse=True)

    def test_zero_market_value_skipped(self):
        positions = [
            _make_position("AAPL", market_val=5000),
            _make_position("ZERO", market_val=0),
        ]
        result = calculate_concentration(positions)
        assert len(result.positions) == 1
        assert result.top_position_pct == pytest.approx(100.0, abs=0.1)


# ── Drawdown tests ────────────────────────────────────────────────────────

class TestDrawdown:
    def test_empty_positions(self):
        result = calculate_drawdown([])
        assert result.max_drawdown_pct == 0.0

    def test_no_drawdown(self):
        """Current value equals cost → no drawdown."""
        positions = [_make_position("AAPL", qty=100, cost_price=10, market_val=1000)]
        result = calculate_drawdown(positions)
        assert result.max_drawdown_pct == pytest.approx(0.0, abs=0.01)

    def test_loss_drawdown(self):
        """Current value < cost → drawdown from cost to current."""
        positions = [_make_position("AAPL", qty=100, cost_price=10, average_cost=10, market_val=800)]
        result = calculate_drawdown(positions)
        # peak=1000 (cost), trough=800 (current), dd = 200/1000 = 20%
        assert result.max_drawdown_pct == pytest.approx(20.0, abs=0.5)
        assert result.max_drawdown_val == pytest.approx(200.0, abs=1)

    def test_gain_no_drawdown(self):
        """Current value > cost → drawdown is from current back to cost."""
        positions = [_make_position("AAPL", qty=100, cost_price=10, average_cost=10, market_val=1500)]
        result = calculate_drawdown(positions)
        # peak=1500 (current), trough=1000 (cost), dd = 500/1500 = 33.3%
        assert result.max_drawdown_pct == pytest.approx(33.33, abs=1)

    def test_with_price_history(self, three_positions, price_history):
        result = calculate_drawdown(three_positions, price_history)
        # Should compute time-series drawdown
        assert result.max_drawdown_pct >= 0
        assert result.max_drawdown_val >= 0
        assert result.peak_val >= result.trough_val


# ── VaR tests ─────────────────────────────────────────────────────────────

class TestVaR:
    def test_empty_positions(self):
        result = calculate_var([])
        assert result.var_95_pct == 0.0

    def test_monte_carlo_var(self, three_positions):
        """Monte Carlo VaR (no price history)."""
        result = calculate_var(three_positions, n_simulations=5000)
        assert result.var_95_pct > 0
        assert result.var_95_val > 0
        assert result.var_99_pct > result.var_95_pct  # 99% VaR > 95% VaR
        assert result.simulation_count == 5000

    def test_historical_var(self, three_positions, price_history):
        """Historical VaR from price history."""
        result = calculate_var(three_positions, price_history)
        assert result.var_95_pct > 0
        assert result.var_95_val > 0
        assert result.cvar_95_pct >= result.var_95_pct  # CVaR >= VaR

    def test_cvar_gte_var(self, three_positions, price_history):
        """CVaR should always be >= VaR (tail average)."""
        result = calculate_var(three_positions, price_history)
        assert result.cvar_95_pct >= result.var_95_pct


# ── Beta tests ────────────────────────────────────────────────────────────

class TestBeta:
    def test_empty_positions(self):
        result = calculate_beta([])
        assert result.portfolio_beta == 0.0

    def test_default_beta(self, three_positions):
        """Without stock_betas, default to 1.0."""
        result = calculate_beta(three_positions)
        assert result.portfolio_beta == pytest.approx(1.0, abs=0.01)

    def test_custom_betas(self, three_positions):
        """Weighted average with custom betas."""
        betas = {"AAPL": 1.2, "NVDA": 1.8, "TSLA": 2.0}
        result = calculate_beta(three_positions, stock_betas=betas)
        # Weighted: 0.5*1.2 + 0.3*1.8 + 0.2*2.0 = 0.6 + 0.54 + 0.4 = 1.54
        assert result.portfolio_beta == pytest.approx(1.54, abs=0.02)
        assert len(result.per_position) == 3

    def test_with_price_history(self, three_positions, price_history):
        """Beta from regression against benchmark."""
        # Create a benchmark that moves similarly to AAPL
        benchmark = price_history["AAPL"]
        result = calculate_beta(
            three_positions,
            price_history=price_history,
            benchmark_history=benchmark,
        )
        # Portfolio is correlated with benchmark, beta should be positive
        assert result.portfolio_beta > 0


# ── Sharpe tests ──────────────────────────────────────────────────────────

class TestSharpe:
    def test_empty_positions(self):
        result = calculate_sharpe([])
        assert result.sharpe_ratio == 0.0

    def test_direct_params(self):
        """Sharpe from pre-computed return and volatility."""
        result = calculate_sharpe(
            [],
            annualized_return=0.10,  # 10% return
            annualized_volatility=0.15,  # 15% vol
            risk_free_rate=0.04,  # 4% risk-free
        )
        # Sharpe = (10% - 4%) / 15% = 0.4
        assert result.sharpe_ratio == pytest.approx(0.4, abs=0.01)

    def test_from_price_history(self, three_positions, price_history):
        """Sharpe computed from daily returns."""
        result = calculate_sharpe(three_positions, price_history, risk_free_rate=0.04)
        assert result.annualized_return_pct != 0.0
        assert result.annualized_volatility_pct > 0
        assert result.period_days > 0

    def test_zero_volatility(self):
        """Zero volatility → sharpe = 0 (avoid division by zero)."""
        result = calculate_sharpe(
            [],
            annualized_return=0.10,
            annualized_volatility=0.0,
        )
        assert result.sharpe_ratio == 0.0


# ── Cost basis tests ─────────────────────────────────────────────────────

class TestCostBasis:
    def test_empty_positions(self):
        result = calculate_cost_basis([])
        assert result.total_cost == 0.0

    def test_single_position(self):
        positions = [_make_position("AAPL", qty=100, average_cost=10, market_val=1200)]
        result = calculate_cost_basis(positions)
        assert result.total_cost == pytest.approx(1000.0, abs=0.1)
        assert result.total_market_val == pytest.approx(1200.0, abs=0.1)
        assert result.total_unrealized_pl == pytest.approx(200.0, abs=0.1)
        assert result.total_unrealized_pl_pct == pytest.approx(20.0, abs=0.5)

    def test_multiple_positions(self, three_positions):
        result = calculate_cost_basis(three_positions)
        total_cost = sum(p.qty * p.average_cost for p in three_positions)
        total_mv = sum(p.market_val for p in three_positions)
        assert result.total_cost == pytest.approx(total_cost, abs=1)
        assert result.total_market_val == pytest.approx(total_mv, abs=1)
        assert len(result.per_position) == 3

    def test_loss_position(self):
        positions = [_make_position("AAPL", qty=100, average_cost=20, market_val=1500)]
        result = calculate_cost_basis(positions)
        assert result.total_unrealized_pl < 0  # Loss
        assert result.total_unrealized_pl_pct < 0

    def test_fallback_to_cost_price(self):
        """If average_cost is 0, should fall back to cost_price."""
        positions = [_make_position("AAPL", qty=100, cost_price=15, average_cost=0, market_val=2000)]
        result = calculate_cost_basis(positions)
        assert result.total_cost == pytest.approx(1500.0, abs=1)


# ── Integration: generate_risk_report ────────────────────────────────────

class TestGenerateRiskReport:
    def test_empty_positions(self):
        report = generate_risk_report([])
        assert report.position_count == 0
        assert report.total_market_val == 0.0
        assert report.generated_at != ""

    def test_full_report(self, three_positions, price_history):
        benchmark = price_history["AAPL"]
        betas = {"AAPL": 1.2, "NVDA": 1.8, "TSLA": 2.0}
        report = generate_risk_report(
            three_positions,
            price_history=price_history,
            benchmark_history=benchmark,
            stock_betas=betas,
        )

        assert report.position_count == 3
        assert report.total_market_val == pytest.approx(10000, abs=1)
        assert report.concentration is not None
        assert report.drawdown is not None
        assert report.var is not None
        assert report.beta is not None
        assert report.sharpe is not None
        assert report.cost_basis is not None

        # Concentration
        assert report.concentration.hhi > 0

        # VaR
        assert report.var.var_95_pct > 0

        # Beta
        assert report.beta.portfolio_beta > 0

        # Sharpe
        assert report.sharpe.annualized_volatility_pct > 0

        # Cost basis
        assert report.cost_basis.total_cost > 0
        assert len(report.cost_basis.per_position) == 3

    def test_to_dict(self, three_positions):
        report = generate_risk_report(three_positions)
        d = report.to_dict()
        assert isinstance(d, dict)
        assert "concentration" in d
        assert "drawdown" in d
        assert "var" in d
        assert "beta" in d
        assert "sharpe" in d
        assert "cost_basis" in d
        assert "generated_at" in d
