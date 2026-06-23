"""Strategy Analytics Service — 策略分析服务

Provides:
1. Strategy backtest with GlobalEquityEngine (佣金/印花税/滑点)
2. Strategy scoring (Sharpe/胜率/最大回撤)
3. Shadow Account integration (实际 vs 理想表现)
4. Market regime heatmap (策略推荐度)

Phase 13+: Strategy Management Enhancement
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from tradingagents.backtest.engines import GlobalEquityEngine, create_engine, get_commission_rate
from tradingagents.dataflows.quant_metrics import QuantMetrics
from tradingagents.models.constant import Market

logger = logging.getLogger(__name__)


@dataclass
class StrategyBacktestResult:
    """策略回测结果"""
    strategy_name: str
    market: str
    period: str                    # e.g. "2026-01-01 ~ 2026-06-24"
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0         # 胜率
    total_pnl: float = 0.0       # 总盈亏
    max_drawdown: float = 0.0    # 最大回撤
    sharpe_ratio: float = 0.0    # Sharpe 比率
    sortino_ratio: float = 0.0   # Sortino 比率
    calmar_ratio: float = 0.0    # Calmar 比率
    avg_holding_days: float = 0.0  # 平均持仓天数
    profit_factor: float = 0.0   # 盈亏比
    commission_paid: float = 0.0  # 已付佣金
    slippage_cost: float = 0.0   # 滑点成本
    equity_curve: list[float] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StrategyScore:
    """策略评分"""
    strategy_name: str
    market: str
    overall_score: float = 0.0   # 综合评分 (0-100)
    sharpe_score: float = 0.0    # Sharpe 评分
    win_rate_score: float = 0.0  # 胜率评分
    drawdown_score: float = 0.0  # 回撤评分
    consistency_score: float = 0.0  # 一致性评分
    risk_adjusted_return: float = 0.0  # 风险调整后收益
    grade: str = "B"             # A/B/C/D/F


@dataclass
class ShadowComparison:
    """Shadow Account 对比"""
    strategy_name: str
    market: str
    actual_trades: int = 0       # 实际交易数
    ideal_trades: int = 0        # 理想交易数
    actual_pnl: float = 0.0     # 实际盈亏
    ideal_pnl: float = 0.0      # 理想盈亏
    delta_pnl: float = 0.0      # 差异
    missed_signals: int = 0      # 错过信号
    noise_trades: int = 0        # 噪音交易
    early_exits: int = 0         # 过早出场
    late_exits: int = 0          # 过晚出场
    execution_quality: float = 0.0  # 执行质量 (0-100)


@dataclass
class HeatmapCell:
    """热力图单元格"""
    strategy_name: str
    market_regime: str           # trending_up, trending_down, ranging, volatile
    suitability_score: float = 0.0  # 适配度 (0-100)
    historical_performance: float = 0.0  # 历史表现
    current_recommendation: str = "neutral"  # strong_buy, buy, neutral, sell, strong_sell


class StrategyAnalyticsService:
    """策略分析服务"""

    def __init__(self):
        self.metrics = QuantMetrics()
        self._backtest_cache: dict[str, StrategyBacktestResult] = {}

    def run_backtest(
        self,
        strategy_name: str,
        trades: list[dict[str, Any]],
        market: str = "HK",
        initial_capital: float = 1_000_000.0,
    ) -> StrategyBacktestResult:
        """Run backtest for a strategy with real market costs.

        Args:
            strategy_name: Strategy identifier.
            trades: List of trade dicts with keys:
                - symbol: str (e.g. "HK.00700")
                - direction: int (1 for buy, -1 for sell)
                - price: float
                - size: float
                - datetime: str (ISO format)
            market: "HK" or "US"
            initial_capital: Starting capital

        Returns:
            StrategyBacktestResult with all metrics.
        """
        engine = create_engine("composite")
        result = StrategyBacktestResult(
            strategy_name=strategy_name,
            market=market,
            period=f"{trades[0]['datetime'][:10]} ~ {trades[-1]['datetime'][:10]}" if trades else "N/A",
        )

        capital = initial_capital
        equity_curve = [capital]
        position: dict[str, Any] = {}  # symbol -> {size, price, direction}
        daily_returns: list[float] = []
        prev_equity = capital

        for trade in trades:
            symbol = trade["symbol"]
            direction = trade["direction"]
            price = trade["price"]
            size = trade["size"]

            # Apply slippage (CompositeEngine uses symbol)
            exec_price = engine.apply_slippage(price, direction, symbol)

            # Calculate commission (CompositeEngine uses symbol)
            is_open = symbol not in position
            commission = engine.calc_commission(size, exec_price, direction, is_open, symbol)

            # Round size (CompositeEngine uses symbol)
            size = engine.round_size(size, exec_price, symbol)

            if is_open:
                # Open position
                cost = size * exec_price + commission
                capital -= cost
                position[symbol] = {
                    "size": size,
                    "price": exec_price,
                    "direction": direction,
                    "commission": commission,
                }
            else:
                # Close position
                pos = position.pop(symbol)
                pnl = (exec_price - pos["price"]) * pos["direction"] * size
                pnl -= commission + pos["commission"]
                capital += size * exec_price - commission
                result.total_pnl += pnl
                result.commission_paid += commission + pos["commission"]
                result.slippage_cost += abs(exec_price - price) * size

                if pnl > 0:
                    result.winning_trades += 1
                else:
                    result.losing_trades += 1

                result.trades.append({
                    "symbol": symbol,
                    "direction": direction,
                    "entry_price": pos["price"],
                    "exit_price": exec_price,
                    "size": size,
                    "pnl": pnl,
                    "commission": commission + pos["commission"],
                })

            equity_curve.append(capital)
            result.total_trades += 1

            # Daily return (simplified)
            if len(equity_curve) > 1:
                daily_ret = (equity_curve[-1] - equity_curve[-2]) / equity_curve[-2]
                daily_returns.append(daily_ret)

        # Calculate metrics
        result.equity_curve = equity_curve
        result.win_rate = result.winning_trades / max(result.total_trades, 1)

        if daily_returns:
            returns_series = pd.Series(daily_returns)
            result.sharpe_ratio = self.metrics.sharpe_ratio(daily_returns, market=market)
            result.sortino_ratio = self.metrics.sortino_ratio(daily_returns, market=market)

            # Max drawdown
            peak = equity_curve[0]
            max_dd = 0.0
            for eq in equity_curve:
                if eq > peak:
                    peak = eq
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
            result.max_drawdown = max_dd

            # Calmar ratio
            if result.max_drawdown > 0:
                annual_return = (equity_curve[-1] / equity_curve[0]) ** (252 / max(len(daily_returns), 1)) - 1
                result.calmar_ratio = annual_return / result.max_drawdown

        # Profit factor
        gross_profit = sum(t["pnl"] for t in result.trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in result.trades if t["pnl"] < 0))
        result.profit_factor = gross_profit / max(gross_loss, 1)

        # Cache result
        cache_key = f"{strategy_name}_{market}"
        self._backtest_cache[cache_key] = result

        return result

    def calculate_score(self, backtest_result: StrategyBacktestResult) -> StrategyScore:
        """Calculate strategy score from backtest results.

        Scoring formula:
        - Sharpe (30%): min(sharpe * 20, 30)
        - Win Rate (25%): win_rate * 25
        - Drawdown (25%): max(0, 25 - max_drawdown * 100)
        - Consistency (20%): profit_factor * 5 (capped at 20)
        """
        score = StrategyScore(
            strategy_name=backtest_result.strategy_name,
            market=backtest_result.market,
        )

        # Sharpe score (30%)
        score.sharpe_score = min(backtest_result.sharpe_ratio * 20, 30)

        # Win rate score (25%)
        score.win_rate_score = backtest_result.win_rate * 25

        # Drawdown score (25%) - lower is better
        score.drawdown_score = max(0, 25 - backtest_result.max_drawdown * 100)

        # Consistency score (20%)
        score.consistency_score = min(backtest_result.profit_factor * 5, 20)

        # Overall score
        score.overall_score = (
            score.sharpe_score +
            score.win_rate_score +
            score.drawdown_score +
            score.consistency_score
        )

        # Grade
        if score.overall_score >= 80:
            score.grade = "A"
        elif score.overall_score >= 60:
            score.grade = "B"
        elif score.overall_score >= 40:
            score.grade = "C"
        elif score.overall_score >= 20:
            score.grade = "D"
        else:
            score.grade = "F"

        return score

    def compare_shadow(
        self,
        strategy_name: str,
        actual_trades: list[dict[str, Any]],
        ideal_signals: list[dict[str, Any]],
        market: str = "HK",
    ) -> ShadowComparison:
        """Compare actual trades with ideal signals.

        Args:
            strategy_name: Strategy identifier.
            actual_trades: Actual executed trades.
            ideal_signals: Ideal signals from strategy.
            market: "HK" or "US"

        Returns:
            ShadowComparison with delta analysis.
        """
        comparison = ShadowComparison(
            strategy_name=strategy_name,
            market=market,
        )

        # Match actual trades to ideal signals
        actual_set = {(t["symbol"], t["datetime"][:10]) for t in actual_trades}
        ideal_set = {(s["symbol"], s["datetime"][:10]) for s in ideal_signals}

        # Missed signals (ideal but not executed)
        missed = ideal_set - actual_set
        comparison.missed_signals = len(missed)

        # Noise trades (executed but not ideal)
        noise = actual_set - ideal_set
        comparison.noise_trades = len(noise)

        # Calculate PnL
        engine = create_engine("composite")

        # Actual PnL
        for trade in actual_trades:
            commission = engine.calc_commission(
                trade["size"], trade["price"], trade["direction"],
                True, trade["symbol"]
            )
            pnl = trade.get("pnl", 0) - commission
            comparison.actual_pnl += pnl

        # Ideal PnL (assuming ideal execution)
        for signal in ideal_signals:
            commission = engine.calc_commission(
                signal["size"], signal["price"], signal["direction"],
                True, signal["symbol"]
            )
            pnl = signal.get("pnl", 0) - commission
            comparison.ideal_pnl += pnl

        comparison.delta_pnl = comparison.actual_pnl - comparison.ideal_pnl
        comparison.actual_trades = len(actual_trades)
        comparison.ideal_trades = len(ideal_signals)

        # Execution quality
        if comparison.ideal_trades > 0:
            match_rate = 1 - (comparison.missed_signals + comparison.noise_trades) / max(comparison.ideal_trades, 1)
            comparison.execution_quality = max(0, match_rate * 100)

        return comparison

    def generate_heatmap(
        self,
        strategies: list[dict[str, Any]],
        market_regimes: Optional[list[str]] = None,
    ) -> list[HeatmapCell]:
        """Generate strategy heatmap by market regime.

        Args:
            strategies: List of strategy metadata dicts.
            market_regimes: List of regimes to include.
                Default: ["trending_up", "trending_down", "ranging", "volatile"]

        Returns:
            List of HeatmapCell for visualization.
        """
        if market_regimes is None:
            market_regimes = ["trending_up", "trending_down", "ranging", "volatile"]

        cells = []
        for strategy in strategies:
            name = strategy.get("name", "")
            suitable_regimes = strategy.get("market_regimes", [])
            priority = strategy.get("default_priority", 100)

            for regime in market_regimes:
                cell = HeatmapCell(
                    strategy_name=name,
                    market_regime=regime,
                )

                # Base suitability from strategy definition
                if regime in suitable_regimes:
                    cell.suitability_score = max(0, 100 - priority)
                else:
                    cell.suitability_score = max(0, 50 - priority / 2)

                # Check cache for historical performance
                cache_key = f"{name}_HK"  # Default to HK
                if cache_key in self._backtest_cache:
                    backtest = self._backtest_cache[cache_key]
                    cell.historical_performance = backtest.sharpe_ratio * 20  # Scale to 0-100

                # Current recommendation
                if cell.suitability_score >= 80:
                    cell.current_recommendation = "strong_buy"
                elif cell.suitability_score >= 60:
                    cell.current_recommendation = "buy"
                elif cell.suitability_score >= 40:
                    cell.current_recommendation = "neutral"
                elif cell.suitability_score >= 20:
                    cell.current_recommendation = "sell"
                else:
                    cell.current_recommendation = "strong_sell"

                cells.append(cell)

        return cells


# Singleton
_strategy_analytics: Optional[StrategyAnalyticsService] = None


def get_strategy_analytics() -> StrategyAnalyticsService:
    """Get singleton StrategyAnalyticsService."""
    global _strategy_analytics
    if _strategy_analytics is None:
        _strategy_analytics = StrategyAnalyticsService()
    return _strategy_analytics
