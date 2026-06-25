"""
tradingagents.data — Data loading and management for TradingAgents-Futu.

Provides DataLoaderProtocol, FutuLoader, and LoaderRegistry for
unified data access with automatic fallback.

Phase 13.3: DataLoader Protocol
Phase P1-5: Unified SQLite data layer (database module)
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
from tradingagents.data.database import (
    BarDataDB,
    DataOverview,
    get_bars,
    get_bars_as_dataframe,
    get_missing_ranges,
    get_overview,
    init_db,
    is_cached,
    upsert_bars,
    upsert_bars_batch,
)

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
    # Database (P1-5)
    "BarDataDB",
    "DataOverview",
    "get_bars",
    "get_bars_as_dataframe",
    "get_missing_ranges",
    "get_overview",
    "init_db",
    "is_cached",
    "upsert_bars",
    "upsert_bars_batch",
]
