"""Quantitative performance metrics for portfolio/trading evaluation.

Pure-Python implementations (numpy/pandas optional) of standard risk/return
metrics used in quantitative finance.  All public functions live on the
``QuantMetrics`` class so they can be imported once and called from any
context (CLI, API, notebooks).

Phase 13.1: Market-aware annualization (HK=245, US=252, CN=244, CRYPTO=365).

Typical usage::

    from tradingagents.dataflows.quant_metrics import QuantMetrics

    qm = QuantMetrics()
    dd  = qm.max_drawdown([100, 105, 98, 110, 102])
    sr  = qm.sharpe_ratio([0.01, -0.005, 0.008, 0.003], market="HK")
"""

from __future__ import annotations

import math
from typing import List, Optional


# ── Market-specific trading days ─────────────────────────────────────────────
TRADING_DAYS_PER_YEAR = {
    "HK": 245,
    "US": 252,
    "CN": 244,
    "CRYPTO": 365,
}


def _trading_days(market: Optional[str] = None) -> int:
    """Get trading days per year for a market.

    Args:
        market: "HK", "US", "CN", "CRYPTO", or None (default US=252).

    Returns:
        int: Trading days per year.
    """
    if market is None:
        return 252
    return TRADING_DAYS_PER_YEAR.get(market.upper(), 252)


class QuantMetrics:
    """Stateless container for quantitative performance calculations.

    Every method is a pure function — inputs go in, a number comes out.
    No side-effects, no hidden state, trivially testable.
    """

    # ── 6.2  max_drawdown ─────────────────────────────────────────────────
    @staticmethod
    def max_drawdown(equity_curve: List[float]) -> float:
        """Maximum drawdown from an equity curve.

        Parameters
        ----------
        equity_curve : list[float]
            Time-ordered portfolio values (e.g. daily net-asset values).

        Returns
        -------
        float
            Maximum drawdown as a ratio in [0, 1].  0 means no drawdown;
            0.25 means the portfolio fell 25 % from its peak.

        Raises
        ------
        ValueError
            If fewer than 2 data points are provided.
        """
        if len(equity_curve) < 2:
            raise ValueError("equity_curve must have at least 2 data points")

        peak = equity_curve[0]
        max_dd = 0.0

        for value in equity_curve:
            if value > peak:
                peak = value
            dd = (peak - value) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        return max_dd

    # ── 6.3  sharpe_ratio ─────────────────────────────────────────────────
    @staticmethod
    def sharpe_ratio(returns: List[float], rf_rate: float = 0.0, market: Optional[str] = None) -> float:
        """Annualised Sharpe ratio.

        Parameters
        ----------
        returns : list[float]
            Daily simple returns (e.g. 0.01 = +1 %).
        rf_rate : float
            Daily risk-free rate.  Default 0.
        market : str, optional
            Market identifier ("HK", "US", "CN", "CRYPTO").
            Determines annualization factor. Default US (252 days).

        Returns
        -------
        float
            Annualised Sharpe ratio.

        Raises
        ------
        ValueError
            If fewer than 2 returns are provided.
        """
        if len(returns) < 2:
            raise ValueError("returns must have at least 2 data points")

        excess = [r - rf_rate for r in returns]
        mean_excess = sum(excess) / len(excess)
        variance = sum((r - mean_excess) ** 2 for r in excess) / (len(excess) - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0

        if std == 0.0:
            return 0.0

        return (mean_excess / std) * math.sqrt(_trading_days(market))

    # ── 6.4  sortino_ratio / win_rate / calmar_ratio ──────────────────────
    @staticmethod
    def sortino_ratio(returns: List[float], rf_rate: float = 0.0, market: Optional[str] = None) -> float:
        """Annualised Sortino ratio (downside deviation only).

        Parameters
        ----------
        returns : list[float]
            Daily simple returns.
        rf_rate : float
            Daily risk-free rate.
        market : str, optional
            Market identifier ("HK", "US", "CN", "CRYPTO").
            Determines annualization factor. Default US (252 days).

        Returns
        -------
        float
            Annualised Sortino ratio.

        Raises
        ------
        ValueError
            If fewer than 2 returns are provided.
        """
        if len(returns) < 2:
            raise ValueError("returns must have at least 2 data points")

        excess = [r - rf_rate for r in returns]
        mean_excess = sum(excess) / len(excess)

        # Only count negative excess returns for downside deviation
        downside_sq = [min(0.0, r) ** 2 for r in excess]
        downside_var = sum(downside_sq) / (len(excess) - 1)
        downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0

        if downside_std == 0.0:
            return 0.0

        return (mean_excess / downside_std) * math.sqrt(_trading_days(market))

    @staticmethod
    def win_rate(returns: List[float]) -> float:
        """Win rate: fraction of trades with positive returns.

        Parameters
        ----------
        returns : list[float]
            Per-trade simple returns.

        Returns
        -------
        float
            Ratio in [0, 1].  0.6 means 60 % of trades were profitable.

        Raises
        ------
        ValueError
            If returns list is empty.
        """
        if not returns:
            raise ValueError("returns must not be empty")

        wins = sum(1 for r in returns if r > 0)
        return wins / len(returns)

    @staticmethod
    def calmar_ratio(
        equity_curve: List[float],
        rf_rate: float = 0.0,
        trading_days: Optional[int] = None,
        market: Optional[str] = None,
    ) -> float:
        """Calmar ratio: annualised return / max drawdown.

        Parameters
        ----------
        equity_curve : list[float]
            Time-ordered portfolio values.
        rf_rate : float
            Annual risk-free rate (not daily — this differs from sharpe/sortino
            because Calmar is conventionally quoted with annual risk-free).
        trading_days : int, optional
            Number of trading days assumed per year. Overrides market if set.
        market : str, optional
            Market identifier ("HK", "US", "CN", "CRYPTO").
            Determines trading days. Default US (252 days).

        Returns
        -------
        float
            Calmar ratio.  Higher is better.

        Raises
        ------
        ValueError
            If fewer than 2 data points or max drawdown is zero.
        """
        if len(equity_curve) < 2:
            raise ValueError("equity_curve must have at least 2 data points")

        # Determine trading days: explicit param > market > default 252
        days = trading_days if trading_days is not None else _trading_days(market)

        # Annualised return from first to last
        total_return = equity_curve[-1] / equity_curve[0] - 1.0
        n_days = len(equity_curve) - 1
        annual_return = (1.0 + total_return) ** (days / n_days) - 1.0

        mdd = QuantMetrics.max_drawdown(equity_curve)
        if mdd == 0.0:
            return 0.0

        return (annual_return - rf_rate) / mdd
