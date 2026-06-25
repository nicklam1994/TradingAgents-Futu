# -*- coding: utf-8 -*-
"""Unified SQLite data layer for TradingAgents-Futu.

Consolidates scattered data (JSON files, in-memory, multiple DB tables) into
a single SQLite database with two new tables:

- ``bar_data`` — OHLCV K-line data (symbol, date, open, high, low, close, volume)
- ``data_overview`` — Tracks which date ranges are already cached (BarOverview)

Preserves existing ``stock_resolver`` tables (plates/stocks/stock_plates) from
``api.database``.

Design principles (vnpy-inspired):
- BarOverview prevents redundant API fetches by recording cached date ranges
- Thread-safe via SQLAlchemy with WAL mode
- Atomic upserts with ON CONFLICT DO UPDATE
- Migration path from stock_universe.json

Phase: P1-5 数据统一到 SQLite
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

logger = logging.getLogger(__name__)

# ── Database Setup ────────────────────────────────────────────────────────────

# Default path: same directory as this file, under data/ subdirectory
_DEFAULT_DB_DIR = Path(__file__).resolve().parent
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "tradingagents.db"

DATABASE_URL = os.getenv("TAF_DATA_DB_URL", f"sqlite:///{_DEFAULT_DB_PATH}")

# Dual-engine architecture:
# - This module uses one engine for bar_data and data_overview tables
# - The api.database module uses a separate engine for stock_resolver tables (plates/stocks)
# - Both can coexist because they serve different purposes:
#   * bar_data: high-frequency time-series writes (K-line data)
#   * stock_resolver: low-frequency reference data (stock universe)
# - WAL mode enables concurrent reads during writes
# - Each engine manages its own connection pool independently

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    echo=False,
    pool_pre_ping=True,
)

# Enable WAL mode for SQLite (better concurrency)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Lazy init flag — tables are created on first DB access, not at import time
_db_initialized = False


@contextmanager
def get_db_session():
    """Context manager for database sessions with auto-commit/rollback."""
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── Data Tables ───────────────────────────────────────────────────────────────

class BarDataDB(Base):
    """OHLCV K-line data cache.

    Stores daily K-line data fetched from Futu/yfinance to avoid redundant
    API calls. Each row is one day's OHLCV for one symbol.
    """

    __tablename__ = "bar_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)     # e.g. "00700.HK", "AAPL"
    date = Column(String(10), nullable=False)                   # YYYY-MM-DD
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False, default=0.0)
    turnover = Column(Float, nullable=True, default=0.0)        # 成交额 (optional)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_bar_symbol_date"),
        Index("ix_bar_symbol_date", "symbol", "date"),
    )


class DataOverview(Base):
    """Tracks which date ranges are already cached for each symbol.

    BarOverview concept (from vnpy): before fetching data from an API,
    check this table to determine which date ranges are missing and only
    fetch those. This avoids redundant API calls.

    Each row represents a contiguous cached range [start_date, end_date]
    for one symbol. Ranges may be merged when new data fills gaps.
    """

    __tablename__ = "data_overview"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    start_date = Column(String(10), nullable=False)             # Earliest cached date
    end_date = Column(String(10), nullable=False)               # Latest cached date
    bar_count = Column(Integer, nullable=False, default=0)      # Number of bars in range
    last_updated = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc))
    source = Column(String(32), nullable=True, default="futu")  # Data source identifier

    __table_args__ = (
        UniqueConstraint("symbol", "source", name="uq_overview_symbol_source"),
        Index("ix_overview_symbol", "symbol"),
    )


# ── Initialization ────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables if they don't exist.

    Safe to call multiple times (idempotent).
    """
    Base.metadata.create_all(bind=engine)
    logger.info("[DataDB] Tables created/verified at %s", DATABASE_URL)


# ── BarData CRUD ──────────────────────────────────────────────────────────────

def upsert_bars(
    symbol: str,
    bars: List[Dict],
    source: str = "futu",
) -> int:
    """Insert or update OHLCV bars for a symbol.

    Uses native SQL INSERT OR REPLACE for efficient upserts instead of
    per-row query + update/insert pattern.

    Args:
        symbol: Canonical symbol (e.g. "00700.HK", "AAPL")
        bars: List of dicts with keys: date, open, high, low, close, volume
              Optional: turnover
        source: Data source identifier for overview tracking

    Returns:
        Number of rows upserted.
    """
    if not bars:
        return 0

    count = 0
    with get_db_session() as db:
        # Use native SQL INSERT OR REPLACE for efficient batch upsert
        for bar in bars:
            db.execute(
                text("""
                    INSERT OR REPLACE INTO bar_data (symbol, date, open, high, low, close, volume, turnover)
                    VALUES (:symbol, :date, :open, :high, :low, :close, :volume, :turnover)
                """),
                {
                    "symbol": symbol,
                    "date": bar["date"],
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar.get("volume", 0.0),
                    "turnover": bar.get("turnover", 0.0),
                },
            )
            count += 1

        # Update data overview
        _update_overview(db, symbol, bars, source)

    logger.debug("[DataDB] Upserted %d bars for %s", count, symbol)
    return count


def get_bars(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict]:
    """Retrieve cached OHLCV bars for a symbol.

    Args:
        symbol: Canonical symbol
        start_date: Optional start date filter (YYYY-MM-DD)
        end_date: Optional end date filter (YYYY-MM-DD)

    Returns:
        List of dicts sorted by date ascending.
    """
    with get_db_session() as db:
        query = db.query(BarDataDB).filter(BarDataDB.symbol == symbol)
        if start_date:
            query = query.filter(BarDataDB.date >= start_date)
        if end_date:
            query = query.filter(BarDataDB.date <= end_date)
        query = query.order_by(BarDataDB.date.asc())

        return [
            {
                "date": row.date,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "turnover": row.turnover or 0.0,
            }
            for row in query.all()
        ]


def get_bars_as_dataframe(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> "pd.DataFrame":
    """Retrieve cached bars as a pandas DataFrame.

    Returns:
        DataFrame with DatetimeIndex and columns: open, high, low, close, volume
    """
    import pandas as pd  # noqa: F811

    bars = get_bars(symbol, start_date, end_date)
    if not bars:
        return pd.DataFrame()

    df = pd.DataFrame(bars)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


# ── DataOverview (BarOverview) ────────────────────────────────────────────────

def _update_overview(
    db: Session,
    symbol: str,
    bars: List[Dict],
    source: str = "futu",
) -> None:
    """Update data_overview after upserting bars.

    Merges with existing range if overlapping or adjacent.
    Uses MIN/MAX aggregation instead of COUNT for O(1) performance.
    """
    if not bars:
        return

    dates = sorted(b["date"] for b in bars)
    new_start = dates[0]
    new_end = dates[-1]

    existing = db.query(DataOverview).filter(
        DataOverview.symbol == symbol,
        DataOverview.source == source,
    ).first()

    if existing:
        # Merge ranges: extend start/end if new data goes beyond existing range
        if new_start < existing.start_date:
            existing.start_date = new_start
        if new_end > existing.end_date:
            existing.end_date = new_end
        # Use MIN/MAX aggregation instead of COUNT for O(1) performance
        result = db.execute(
            text("SELECT MIN(date), MAX(date) FROM bar_data WHERE symbol = :symbol"),
            {"symbol": symbol},
        ).first()
        if result and result[0] and result[1]:
            existing.start_date = result[0]
            existing.end_date = result[1]
            # Estimate bar_count from date range (trading days ~ 252/year)
            from datetime import datetime as dt
            start_dt = dt.strptime(result[0], "%Y-%m-%d")
            end_dt = dt.strptime(result[1], "%Y-%m-%d")
            days_diff = (end_dt - start_dt).days
            existing.bar_count = max(1, int(days_diff * 252 / 365))
        existing.last_updated = datetime.now(timezone.utc)
    else:
        db.add(DataOverview(
            symbol=symbol,
            start_date=new_start,
            end_date=new_end,
            bar_count=len(bars),
            source=source,
        ))


def get_overview(symbol: str, source: str = "futu") -> Optional[Dict]:
    """Get the cached data range for a symbol.

    Returns:
        Dict with keys: symbol, start_date, end_date, bar_count, last_updated
        or None if no data cached.
    """
    with get_db_session() as db:
        row = db.query(DataOverview).filter(
            DataOverview.symbol == symbol,
            DataOverview.source == source,
        ).first()
        if not row:
            return None
        return {
            "symbol": row.symbol,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "bar_count": row.bar_count,
            "last_updated": row.last_updated.isoformat() if row.last_updated else None,
            "source": row.source,
        }


def get_missing_ranges(
    symbol: str,
    start_date: str,
    end_date: str,
    source: str = "futu",
) -> List[Tuple[str, str]]:
    """Determine which date ranges need to be fetched from API.

    Compares the requested range [start_date, end_date] with cached ranges
    in data_overview and returns only the missing sub-ranges.

    Returns:
        List of (start, end) tuples for missing ranges. Empty if fully cached.
    """
    overview = get_overview(symbol, source)
    if not overview:
        return [(start_date, end_date)]

    cached_start = overview["start_date"]
    cached_end = overview["end_date"]

    missing = []

    # Gap before cached range
    if start_date < cached_start:
        missing.append((start_date, min(end_date, cached_start)))

    # Gap after cached range
    if end_date > cached_end:
        missing.append((max(start_date, cached_end), end_date))

    return missing


def is_cached(symbol: str, start_date: str, end_date: str, source: str = "futu") -> bool:
    """Check if the full date range is already cached.

    Returns True only if the entire requested range is covered by cached data.
    """
    return len(get_missing_ranges(symbol, start_date, end_date, source)) == 0


# ── Bulk Operations ───────────────────────────────────────────────────────────

def upsert_bars_batch(
    data: Dict[str, List[Dict]],
    source: str = "futu",
) -> int:
    """Upsert bars for multiple symbols at once.

    Args:
        data: Mapping {symbol: [bar_dicts]}
        source: Data source identifier

    Returns:
        Total number of rows upserted.
    """
    total = 0
    for symbol, bars in data.items():
        total += upsert_bars(symbol, bars, source)
    return total


def get_all_overviews() -> List[Dict]:
    """Get all data overview entries.

    Returns:
        List of overview dicts for all cached symbols.
    """
    with get_db_session() as db:
        rows = db.query(DataOverview).order_by(DataOverview.symbol).all()
        return [
            {
                "symbol": row.symbol,
                "start_date": row.start_date,
                "end_date": row.end_date,
                "bar_count": row.bar_count,
                "last_updated": row.last_updated.isoformat() if row.last_updated else None,
                "source": row.source,
            }
            for row in rows
        ]


def clear_cache(symbol: Optional[str] = None) -> int:
    """Clear cached bar data.

    Args:
        symbol: If provided, clear only this symbol's data. If None, clear all.

    Returns:
        Number of bar_data rows deleted.
    """
    with get_db_session() as db:
        if symbol:
            count = db.query(BarDataDB).filter(BarDataDB.symbol == symbol).delete()
            db.query(DataOverview).filter(DataOverview.symbol == symbol).delete()
        else:
            count = db.query(BarDataDB).delete()
            db.query(DataOverview).delete()
        logger.info("[DataDB] Cleared %d bars (symbol=%s)", count, symbol or "ALL")
        return count


# ── Auto-init on import ───────────────────────────────────────────────────────

# Tables are created lazily on first DB access (via get_db_session)
# to avoid import side effects. Call init_db() explicitly if needed.
