"""
tradingagents.data — Data loading and management for TradingAgents-Futu.

Provides DataLoaderProtocol, FutuLoader, and LoaderRegistry for
unified data access with automatic fallback.

Phase 13.3: DataLoader Protocol
"""
from tradingagents.data.loaders import (
    DataLoaderProtocol,
    BaseLoader,
    LoaderRegistry,
    NoAvailableSourceError,
    validate_date_range,
    validate_ohlc,
)
from tradingagents.data.futu_loader import FutuLoader

__all__ = [
    # Protocol & Base
    "DataLoaderProtocol",
    "BaseLoader",
    "LoaderRegistry",
    # Exceptions
    "NoAvailableSourceError",
    # Validation
    "validate_date_range",
    "validate_ohlc",
    # Loaders
    "FutuLoader",
]
