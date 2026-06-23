"""
tradingagents.models — Type-safe data objects for TradingAgents-Futu.

Replaces dict-based data passing with typed dataclasses.
Extracted from api/services/sim_trading_service.py (Phase 13.1).

Usage:
    from tradingagents.models import AccountInfo, Position, OrderInfo, BarData
"""
from tradingagents.models.data_objects import (
    AccountInfo,
    Position,
    OrderResult,
    OrderInfo,
    DealInfo,
    BarData,
    TickData,
    SignalData,
)
from tradingagents.models.constant import (
    Direction,
    OrderStatus,
    OrderType,
    Exchange,
    Market,
    Interval,
    parse_market,
    parse_exchange,
)

__all__ = [
    # Data objects
    "AccountInfo",
    "Position",
    "OrderResult",
    "OrderInfo",
    "DealInfo",
    "BarData",
    "TickData",
    "SignalData",
    # Constants
    "Direction",
    "OrderStatus",
    "OrderType",
    "Exchange",
    "Market",
    "Interval",
    # Helpers
    "parse_market",
    "parse_exchange",
]
