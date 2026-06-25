"""Portfolio Risk Service — concentration, drawdown, VaR, beta, sharpe, cost tracking.

Provides portfolio-level risk analytics for holdings imported from Futu.
All pure-computation functions accept simple data structures (lists/dicts)
so they are easy to test without Futu connectivity.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────

@dataclass
class PositionSnapshot:
    """Single position in the portfolio — mirrors FutuProvider.get_positions() output."""
    symbol: str
    stock_name: str = ""
    qty: float = 0.0
    cost_price: float = 0.0
    average_cost: float = 0.0
    market_val: float = 0.0
    nominal_price: float = 0.0
    pl_ratio: float = 0.0
    pl_val: float = 0.0
    unrealized_pl: float = 0.0
    realized_pl: float = 0.0
    currency: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "PositionSnapshot":
        """Create from FutuProvider.get_positions() dict."""
        return cls(
            symbol=d.get("symbol", ""),
            stock_name=d.get("stock_name", ""),
            qty=float(d.get("qty", 0) or 0),
            cost_price=float(d.get("cost_price", 0) or 0),
            average_cost=float(d.get("average_cost", 0) or 0),
            market_val=float(d.get("market_val", 0) or 0),
            nominal_price=float(d.get("nominal_price", 0) or 0),
            pl_ratio=float(d.get("pl_ratio", 0) or 0),
            pl_val=float(d.get("pl_val", 0) or 0),
            unrealized_pl=float(d.get("unrealized_pl", 0) or 0),
            realized_pl=float(d.get("realized_pl", 0) or 0),
            currency=d.get("currency", ""),
        )


@dataclass
class ConcentrationResult:
    """Concentration analysis output."""
    hhi: float = 0.0                   # Herfindahl-Hirschman Index (0-10000)
    top_position_pct: float = 0.0      # Largest single position weight %
    top3_pct: float = 0.0             # Top-3 combined weight %
    positions: List[Dict[str, Any]] = field(default_factory=list)
    # Each: {symbol, weight_pct, market_val}


@dataclass
class DrawdownResult:
    """Drawdown analysis output."""
    max_drawdown_pct: float = 0.0     # Maximum drawdown %
    max_drawdown_val: float = 0.0     # Maximum drawdown in currency
    peak_val: float = 0.0             # Peak portfolio value
    trough_val: float = 0.0           # Trough portfolio value


@dataclass
class VaRResult:
    """Value at Risk output."""
    var_95_pct: float = 0.0           # 95% VaR as % of portfolio
    var_95_val: float = 0.0           # 95% VaR in currency
    var_99_pct: float = 0.0           # 99% VaR as % of portfolio
    var_99_val: float = 0.0           # 99% VaR in currency
    cvar_95_pct: float = 0.0          # 95% Conditional VaR (Expected Shortfall)
    cvar_95_val: float = 0.0
    simulation_count: int = 0         # Number of Monte Carlo runs


@dataclass
class BetaResult:
    """Portfolio beta output."""
    portfolio_beta: float = 0.0       # Weighted average beta
    per_position: List[Dict[str, Any]] = field(default_factory=list)
    # Each: {symbol, beta, weight_pct, contribution}


@dataclass
class SharpeResult:
    """Sharpe ratio output."""
    sharpe_ratio: float = 0.0
    annualized_return_pct: float = 0.0
    annualized_volatility_pct: float = 0.0
    risk_free_rate_pct: float = 0.0
    period_days: int = 0


@dataclass
class CostBasisResult:
    """Cost basis and P&L tracking."""
    total_cost: float = 0.0          # Total cost basis
    total_market_val: float = 0.0    # Current market value
    total_unrealized_pl: float = 0.0
    total_unrealized_pl_pct: float = 0.0
    per_position: List[Dict[str, Any]] = field(default_factory=list)
    # Each: {symbol, qty, avg_cost, current_price, cost_basis, market_val, unrealized_pl, pl_pct}


@dataclass
class PortfolioRiskReport:
    """Complete portfolio risk report — the primary output of this service."""
    generated_at: str = ""
    total_market_val: float = 0.0
    position_count: int = 0
    concentration: Optional[ConcentrationResult] = None
    drawdown: Optional[DrawdownResult] = None
    var: Optional[VaRResult] = None
    beta: Optional[BetaResult] = None
    sharpe: Optional[SharpeResult] = None
    cost_basis: Optional[CostBasisResult] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-friendly dict."""
        d = asdict(self)
        return d


# ── Core computation functions ────────────────────────────────────────────

def calculate_concentration(positions: List[PositionSnapshot]) -> ConcentrationResult:
    """Calculate portfolio concentration using HHI and top-position analysis.

    HHI = sum of squared weights (0-10000 scale).
    Higher HHI = more concentrated portfolio.

    Args:
        positions: List of position snapshots with market_val.

    Returns:
        ConcentrationResult with HHI, top positions, and per-position weights.
    """
    if not positions:
        return ConcentrationResult()

    total_mv = sum(p.market_val for p in positions if p.market_val > 0)
    if total_mv <= 0:
        return ConcentrationResult()

    # Build per-position weights
    weighted: List[Dict[str, Any]] = []
    for p in positions:
        if p.market_val <= 0:
            continue
        w = p.market_val / total_mv  # fraction 0-1
        weighted.append({
            "symbol": p.symbol,
            "weight_pct": round(w * 100, 4),
            "market_val": round(p.market_val, 2),
        })

    # Sort by weight descending
    weighted.sort(key=lambda x: x["weight_pct"], reverse=True)

    # HHI in 0-10000 scale (sum of squared % weights)
    hhi = sum((w["weight_pct"]) ** 2 for w in weighted)

    # Top-N analysis
    top_pct = weighted[0]["weight_pct"] if weighted else 0.0
    top3_pct = sum(w["weight_pct"] for w in weighted[:3])

    return ConcentrationResult(
        hhi=round(hhi, 2),
        top_position_pct=round(top_pct, 4),
        top3_pct=round(top3_pct, 4),
        positions=weighted,
    )


def calculate_drawdown(
    positions: List[PositionSnapshot],
    price_history: Optional[Dict[str, List[float]]] = None,
) -> DrawdownResult:
    """Calculate maximum drawdown.

    Two modes:
    1. With price_history: uses daily portfolio values to compute drawdown over time.
    2. Without price_history: uses cost_price vs current nominal_price as proxy.
       This is a simplified single-period drawdown (current vs cost).

    Args:
        positions: Current position snapshots.
        price_history: Optional dict {symbol: [daily_close_prices]} for time-series drawdown.

    Returns:
        DrawdownResult with max drawdown % and currency values.
    """
    if not positions:
        return DrawdownResult()

    if price_history:
        return _drawdown_from_history(positions, price_history)

    # Simplified: drawdown from cost basis to current value
    total_cost = sum(p.qty * (p.cost_price or p.average_cost) for p in positions)
    total_mv = sum(p.market_val for p in positions)

    if total_cost <= 0:
        return DrawdownResult()

    # Drawdown = (peak - trough) / peak
    # Here peak = max(cost, current), trough = min(cost, current)
    peak = max(total_cost, total_mv)
    trough = min(total_cost, total_mv)
    dd_val = peak - trough
    dd_pct = (dd_val / peak * 100) if peak > 0 else 0.0

    return DrawdownResult(
        max_drawdown_pct=round(dd_pct, 4),
        max_drawdown_val=round(dd_val, 2),
        peak_val=round(peak, 2),
        trough_val=round(trough, 2),
    )


def _drawdown_from_history(
    positions: List[PositionSnapshot],
    price_history: Dict[str, List[float]],
) -> DrawdownResult:
    """Compute max drawdown from daily price history."""
    # Reconstruct daily portfolio values
    # Assume all symbols have the same number of daily observations
    symbols = [p.symbol for p in positions if p.symbol in price_history]
    if not symbols:
        return DrawdownResult()

    n_days = min(len(price_history[s]) for s in symbols)
    if n_days < 2:
        return DrawdownResult()

    # Compute daily portfolio values using position quantities
    qty_map = {p.symbol: p.qty for p in positions}
    portfolio_values = []
    for i in range(n_days):
        val = sum(qty_map[s] * price_history[s][i] for s in symbols)
        portfolio_values.append(val)

    # Compute running max drawdown
    peak = portfolio_values[0]
    max_dd = 0.0
    max_dd_val = 0.0
    peak_val = peak
    trough_val = peak

    for val in portfolio_values:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
            max_dd_val = dd
            peak_val = peak
            trough_val = val

    dd_pct = (max_dd / peak_val * 100) if peak_val > 0 else 0.0

    return DrawdownResult(
        max_drawdown_pct=round(dd_pct, 4),
        max_drawdown_val=round(max_dd_val, 2),
        peak_val=round(peak_val, 2),
        trough_val=round(trough_val, 2),
    )


def calculate_var(
    positions: List[PositionSnapshot],
    price_history: Optional[Dict[str, List[float]]] = None,
    n_simulations: int = 10000,
    confidence_levels: tuple = (0.95, 0.99),
) -> VaRResult:
    """Calculate Value at Risk using historical simulation.

    Two modes:
    1. With price_history: computes daily returns from history, uses historical simulation.
    2. Without price_history: uses Monte Carlo simulation with assumed parameters.

    Args:
        positions: Current positions.
        price_history: Optional {symbol: [daily_close_prices]}.
        n_simulations: Number of Monte Carlo runs (used when no history).
        confidence_levels: Tuple of confidence levels (default 95%, 99%).

    Returns:
        VaRResult with VaR and CVaR at specified confidence levels.
    """
    if not positions:
        return VaRResult()

    total_mv = sum(p.market_val for p in positions if p.market_val > 0)
    if total_mv <= 0:
        return VaRResult()

    if price_history:
        return _var_from_history(positions, price_history, total_mv, confidence_levels)

    # Monte Carlo simulation (no history available)
    return _var_monte_carlo(positions, total_mv, n_simulations, confidence_levels)


def _var_from_history(
    positions: List[PositionSnapshot],
    price_history: Dict[str, List[float]],
    total_mv: float,
    confidence_levels: tuple,
) -> VaRResult:
    """Compute VaR from historical daily returns."""
    symbols = [p.symbol for p in positions if p.symbol in price_history]
    if not symbols:
        return VaRResult()

    n_days = min(len(price_history[s]) for s in symbols)
    if n_days < 10:
        return VaRResult()

    qty_map = {p.symbol: p.qty for p in positions}

    # Daily portfolio returns
    daily_values = []
    for i in range(n_days):
        val = sum(qty_map[s] * price_history[s][i] for s in symbols)
        daily_values.append(val)

    returns = np.diff(daily_values) / np.array(daily_values[:-1])
    returns = returns[~np.isnan(returns)]

    if len(returns) < 5:
        return VaRResult()

    # Sort returns ascending
    sorted_returns = np.sort(returns)

    result = VaRResult(simulation_count=len(returns))

    for cl in confidence_levels:
        idx = int((1 - cl) * len(sorted_returns))
        idx = max(0, min(idx, len(sorted_returns) - 1))
        var_return = -sorted_returns[idx]
        var_val = var_return * total_mv

        # CVaR (Expected Shortfall) = average of returns beyond VaR
        tail = sorted_returns[:idx + 1]
        cvar_return = -np.mean(tail) if len(tail) > 0 else var_return
        cvar_val = cvar_return * total_mv

        if cl == 0.95:
            result.var_95_pct = round(var_return * 100, 4)
            result.var_95_val = round(var_val, 2)
            result.cvar_95_pct = round(cvar_return * 100, 4)
            result.cvar_95_val = round(cvar_val, 2)
        elif cl == 0.99:
            result.var_99_pct = round(var_return * 100, 4)
            result.var_99_val = round(var_val, 2)

    return result


def _var_monte_carlo(
    positions: List[PositionSnapshot],
    total_mv: float,
    n_simulations: int,
    confidence_levels: tuple,
) -> VaRResult:
    """Monte Carlo VaR when no historical data is available.

    Uses assumed annual volatility of 20% (converted to daily).
    Simulates 1-day portfolio P&L.
    """
    # Assume annual vol 20% → daily vol = 20% / sqrt(252)
    annual_vol = 0.20
    daily_vol = annual_vol / math.sqrt(252)

    # Simulate daily returns (normal distribution)
    rng = np.random.default_rng(seed=42)  # reproducible
    simulated_returns = rng.normal(0, daily_vol, n_simulations)
    simulated_pnl = simulated_returns * total_mv

    sorted_pnl = np.sort(simulated_pnl)

    result = VaRResult(simulation_count=n_simulations)

    for cl in confidence_levels:
        idx = int((1 - cl) * n_simulations)
        idx = max(0, min(idx, n_simulations - 1))
        var_val = -sorted_pnl[idx]
        var_pct = (var_val / total_mv * 100) if total_mv > 0 else 0.0

        tail = sorted_pnl[:idx + 1]
        cvar_val = -np.mean(tail) if len(tail) > 0 else var_val
        cvar_pct = (cvar_val / total_mv * 100) if total_mv > 0 else 0.0

        if cl == 0.95:
            result.var_95_pct = round(var_pct, 4)
            result.var_95_val = round(var_val, 2)
            result.cvar_95_pct = round(cvar_pct, 4)
            result.cvar_95_val = round(cvar_val, 2)
        elif cl == 0.99:
            result.var_99_pct = round(var_pct, 4)
            result.var_99_val = round(var_val, 2)

    return result


def calculate_beta(
    positions: List[PositionSnapshot],
    stock_betas: Optional[Dict[str, float]] = None,
    price_history: Optional[Dict[str, List[float]]] = None,
    benchmark_history: Optional[List[float]] = None,
) -> BetaResult:
    """Calculate portfolio beta (weighted average of individual stock betas).

    Two modes:
    1. With stock_betas: uses provided per-stock betas, computes weighted average.
    2. With price_history + benchmark_history: computes beta from regression.

    Args:
        positions: Current positions.
        stock_betas: Optional {symbol: beta_value} lookup.
        price_history: Optional {symbol: [daily_close_prices]}.
        benchmark_history: Optional [daily_close_prices] for benchmark index.

    Returns:
        BetaResult with portfolio beta and per-position breakdown.
    """
    if not positions:
        return BetaResult()

    total_mv = sum(p.market_val for p in positions if p.market_val > 0)
    if total_mv <= 0:
        return BetaResult()

    # Mode 1: Use provided betas
    if stock_betas:
        return _beta_from_lookup(positions, stock_betas, total_mv)

    # Mode 2: Compute from price history
    if price_history and benchmark_history:
        return _beta_from_regression(positions, price_history, benchmark_history, total_mv)

    # Fallback: assume beta = 1.0 for all positions
    return _beta_from_lookup(positions, {}, total_mv)


def _beta_from_lookup(
    positions: List[PositionSnapshot],
    stock_betas: Dict[str, float],
    total_mv: float,
) -> BetaResult:
    """Weighted-average beta from per-stock beta lookup."""
    default_beta = 1.0
    per_pos = []
    weighted_beta_sum = 0.0

    for p in positions:
        if p.market_val <= 0:
            continue
        w = p.market_val / total_mv
        b = stock_betas.get(p.symbol, default_beta)
        contribution = w * b
        weighted_beta_sum += contribution
        per_pos.append({
            "symbol": p.symbol,
            "beta": round(b, 4),
            "weight_pct": round(w * 100, 4),
            "contribution": round(contribution, 4),
        })

    return BetaResult(
        portfolio_beta=round(weighted_beta_sum, 4),
        per_position=per_pos,
    )


def _beta_from_regression(
    positions: List[PositionSnapshot],
    price_history: Dict[str, List[float]],
    benchmark_history: List[float],
    total_mv: float,
) -> BetaResult:
    """Compute beta from OLS regression of portfolio returns vs benchmark."""
    symbols = [p.symbol for p in positions if p.symbol in price_history]
    if not symbols:
        return BetaResult()

    n_days = min(len(price_history[s]) for s in symbols)
    n_days = min(n_days, len(benchmark_history))
    if n_days < 10:
        return BetaResult()

    qty_map = {p.symbol: p.qty for p in positions}

    # Portfolio daily values
    portfolio_values = []
    for i in range(n_days):
        val = sum(qty_map[s] * price_history[s][i] for s in symbols)
        portfolio_values.append(val)

    # Returns
    port_returns = np.diff(portfolio_values) / np.array(portfolio_values[:-1])
    bench_returns = np.diff(benchmark_history[:n_days]) / np.array(benchmark_history[:n_days - 1])

    # OLS: portfolio_return = alpha + beta * benchmark_return
    cov = np.cov(port_returns, bench_returns)
    if cov.shape == (2, 2) and cov[1, 1] > 0:
        port_beta = cov[0, 1] / cov[1, 1]
    else:
        port_beta = 1.0

    # Per-position contribution (simplified: assume same beta for all)
    qty_map_local = {p.symbol: p.qty for p in positions}
    total_mv_local = sum(p.market_val for p in positions if p.market_val > 0)
    per_pos = []
    for p in positions:
        if p.market_val <= 0:
            continue
        w = p.market_val / total_mv_local if total_mv_local > 0 else 0
        per_pos.append({
            "symbol": p.symbol,
            "beta": round(port_beta, 4),
            "weight_pct": round(w * 100, 4),
            "contribution": round(w * port_beta, 4),
        })

    return BetaResult(
        portfolio_beta=round(port_beta, 4),
        per_position=per_pos,
    )


def calculate_sharpe(
    positions: List[PositionSnapshot],
    price_history: Optional[Dict[str, List[float]]] = None,
    risk_free_rate: float = 0.04,
    annualized_return: Optional[float] = None,
    annualized_volatility: Optional[float] = None,
) -> SharpeResult:
    """Calculate Sharpe ratio.

    Two modes:
    1. With price_history: computes from daily portfolio returns.
    2. With annualized_return + annualized_volatility: direct computation.

    Args:
        positions: Current positions.
        price_history: Optional {symbol: [daily_close_prices]}.
        risk_free_rate: Annual risk-free rate (default 4%).
        annualized_return: Optional pre-computed annualized return.
        annualized_volatility: Optional pre-computed annualized volatility.

    Returns:
        SharpeResult with Sharpe ratio and component metrics.
    """
    if annualized_return is not None and annualized_volatility is not None:
        excess = annualized_return - risk_free_rate
        sharpe = excess / annualized_volatility if annualized_volatility > 0 else 0.0
        return SharpeResult(
            sharpe_ratio=round(sharpe, 4),
            annualized_return_pct=round(annualized_return * 100, 4),
            annualized_volatility_pct=round(annualized_volatility * 100, 4),
            risk_free_rate_pct=round(risk_free_rate * 100, 4),
        )

    if not price_history:
        return SharpeResult(risk_free_rate_pct=round(risk_free_rate * 100, 4))

    symbols = [p.symbol for p in positions if p.symbol in price_history]
    if not symbols:
        return SharpeResult(risk_free_rate_pct=round(risk_free_rate * 100, 4))

    n_days = min(len(price_history[s]) for s in symbols)
    if n_days < 10:
        return SharpeResult(risk_free_rate_pct=round(risk_free_rate * 100, 4))

    qty_map = {p.symbol: p.qty for p in positions}

    # Daily portfolio values
    portfolio_values = []
    for i in range(n_days):
        val = sum(qty_map[s] * price_history[s][i] for s in symbols)
        portfolio_values.append(val)

    daily_returns = np.diff(portfolio_values) / np.array(portfolio_values[:-1])
    daily_returns = daily_returns[~np.isnan(daily_returns)]

    if len(daily_returns) < 5:
        return SharpeResult(risk_free_rate_pct=round(risk_free_rate * 100, 4))

    # Annualize
    mean_daily = np.mean(daily_returns)
    std_daily = np.std(daily_returns, ddof=1)
    ann_return = mean_daily * 252
    ann_vol = std_daily * math.sqrt(252)

    excess_return = ann_return - risk_free_rate
    sharpe = excess_return / ann_vol if ann_vol > 0 else 0.0

    return SharpeResult(
        sharpe_ratio=round(sharpe, 4),
        annualized_return_pct=round(ann_return * 100, 4),
        annualized_volatility_pct=round(ann_vol * 100, 4),
        risk_free_rate_pct=round(risk_free_rate * 100, 4),
        period_days=n_days,
    )


def calculate_cost_basis(positions: List[PositionSnapshot]) -> CostBasisResult:
    """Calculate cost basis, P&L tracking per position and total.

    Uses average_cost from Futu (falls back to cost_price if average_cost is 0).

    Args:
        positions: Current positions with qty, average_cost, market_val.

    Returns:
        CostBasisResult with per-position and aggregate cost/P&L data.
    """
    if not positions:
        return CostBasisResult()

    per_pos = []
    total_cost = 0.0
    total_mv = 0.0
    total_upl = 0.0

    for p in positions:
        if p.qty <= 0:
            continue
        avg_cost = p.average_cost if p.average_cost > 0 else p.cost_price
        cost_basis = p.qty * avg_cost
        mv = p.market_val
        upl = mv - cost_basis
        pl_pct = (upl / cost_basis * 100) if cost_basis > 0 else 0.0
        current_price = mv / p.qty if p.qty > 0 else 0.0

        per_pos.append({
            "symbol": p.symbol,
            "stock_name": p.stock_name,
            "qty": p.qty,
            "avg_cost": round(avg_cost, 4),
            "current_price": round(current_price, 4),
            "cost_basis": round(cost_basis, 2),
            "market_val": round(mv, 2),
            "unrealized_pl": round(upl, 2),
            "pl_pct": round(pl_pct, 4),
            "currency": p.currency,
        })

        total_cost += cost_basis
        total_mv += mv
        total_upl += upl

    total_pl_pct = (total_upl / total_cost * 100) if total_cost > 0 else 0.0

    return CostBasisResult(
        total_cost=round(total_cost, 2),
        total_market_val=round(total_mv, 2),
        total_unrealized_pl=round(total_upl, 2),
        total_unrealized_pl_pct=round(total_pl_pct, 4),
        per_position=per_pos,
    )


# ── High-level orchestrator ──────────────────────────────────────────────

def generate_risk_report(
    positions: List[PositionSnapshot],
    price_history: Optional[Dict[str, List[float]]] = None,
    benchmark_history: Optional[List[float]] = None,
    stock_betas: Optional[Dict[str, float]] = None,
    risk_free_rate: float = 0.04,
) -> PortfolioRiskReport:
    """Generate a complete portfolio risk report.

    This is the primary entry point for the service. It runs all risk
    analyses and returns a unified PortfolioRiskReport.

    Args:
        positions: List of position snapshots (from FutuProvider.get_positions()).
        price_history: Optional {symbol: [daily_close_prices]} for time-series analysis.
        benchmark_history: Optional [daily_close_prices] for benchmark (e.g., HSI, SPY).
        stock_betas: Optional {symbol: beta_value} lookup.
        risk_free_rate: Annual risk-free rate (default 4%).

    Returns:
        PortfolioRiskReport with all risk metrics populated.
    """
    if not positions:
        return PortfolioRiskReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            position_count=0,
        )

    total_mv = sum(p.market_val for p in positions if p.market_val > 0)

    report = PortfolioRiskReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_market_val=round(total_mv, 2),
        position_count=len([p for p in positions if p.qty > 0]),
        concentration=calculate_concentration(positions),
        drawdown=calculate_drawdown(positions, price_history),
        var=calculate_var(positions, price_history),
        beta=calculate_beta(positions, stock_betas, price_history, benchmark_history),
        sharpe=calculate_sharpe(positions, price_history, risk_free_rate),
        cost_basis=calculate_cost_basis(positions),
    )

    return report
