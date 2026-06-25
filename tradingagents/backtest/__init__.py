"""
tradingagents.backtest — Multi-engine backtest for TradingAgents-Futu.

Provides market-specific trading rules (commission, stamp tax, lot size, slippage)
for HK and US stocks/ETFs.

Phase 13.4: Multi-engine backtest
Phase 13.5: Enhanced BaseEngine with bar-by-bar execution, metrics module

Modules:
  - engines: API-facing adapter (simple BaseEngine, CompositeEngine, create_engine)
  - base_engine: Enhanced BaseEngine with 5-step pipeline and execution loop
  - global_equity_engine: HK/US market rules inheriting from enhanced BaseEngine
  - metrics: Sharpe, Sortino, Max Drawdown, Win Rate, Profit Factor
  - models: Position, TradeRecord, EquitySnapshot dataclasses
"""
# API-facing adapter (backward compatible)
from tradingagents.backtest.engines import (
    BaseEngine,
    GlobalEquityEngine,
    CompositeEngine,
    create_engine,
    get_commission_rate,
)

# Enhanced engine with execution loop
from tradingagents.backtest.base_engine import BaseEngine as EnhancedBaseEngine
from tradingagents.backtest.base_engine import align_signals

# Market-specific engine (HK/US)
from tradingagents.backtest.global_equity_engine import (
    GlobalEquityEngine as GlobalEquityEngineV2,
)

# Metrics
from tradingagents.backtest.metrics import (
    calc_metrics,
    calc_bars_per_year,
    win_rate_and_stats,
    by_symbol_stats,
    by_exit_reason_stats,
)

# Data models
from tradingagents.backtest.models import Position, TradeRecord, EquitySnapshot

__all__ = [
    # API-facing (backward compatible)
    "BaseEngine",
    "GlobalEquityEngine",
    "CompositeEngine",
    "create_engine",
    "get_commission_rate",
    # Enhanced engine
    "EnhancedBaseEngine",
    "align_signals",
    # Market-specific
    "GlobalEquityEngineV2",
    # Metrics
    "calc_metrics",
    "calc_bars_per_year",
    "win_rate_and_stats",
    "by_symbol_stats",
    "by_exit_reason_stats",
    # Models
    "Position",
    "TradeRecord",
    "EquitySnapshot",
]
