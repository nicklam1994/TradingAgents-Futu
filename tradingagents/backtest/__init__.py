"""
tradingagents.backtest — Multi-engine backtest for TradingAgents-Futu.

Provides market-specific trading rules (commission, stamp tax, lot size, slippage)
for HK and US stocks/ETFs.

Phase 13.4: Multi-engine backtest
"""
from tradingagents.backtest.engines import (
    BaseEngine,
    GlobalEquityEngine,
    CompositeEngine,
    create_engine,
    get_commission_rate,
)

__all__ = [
    "BaseEngine",
    "GlobalEquityEngine",
    "CompositeEngine",
    "create_engine",
    "get_commission_rate",
]
