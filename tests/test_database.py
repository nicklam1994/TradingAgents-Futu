# -*- coding: utf-8 -*-
"""Tests for tradingagents.data.database — Unified SQLite data layer.

Covers:
- BarDataDB CRUD (upsert, query, date filtering)
- DataOverview (BarOverview) range tracking
- get_missing_ranges() for incremental fetch
- Migration script
- Batch operations

Phase: P1-5 数据统一到 SQLite
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Use a temporary database for each test to ensure isolation."""
    db_path = tmp_path / "test_data.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("TAF_DATA_DB_URL", db_url)

    # Re-import to pick up the new env var
    import importlib
    import tradingagents.data.database as db_module
    importlib.reload(db_module)

    yield db_module

    # Cleanup
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def sample_bars():
    """Sample OHLCV bar data for testing."""
    return [
        {"date": "2026-01-02", "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000000},
        {"date": "2026-01-03", "open": 103.0, "high": 108.0, "low": 102.0, "close": 107.0, "volume": 1200000},
        {"date": "2026-01-06", "open": 107.0, "high": 110.0, "low": 106.0, "close": 109.0, "volume": 900000},
    ]


@pytest.fixture
def sample_bars_extended():
    """Extended bar data for range testing."""
    return [
        {"date": f"2026-01-{d:02d}", "open": 100.0 + d, "high": 105.0 + d, "low": 99.0 + d, "close": 103.0 + d, "volume": 1000000 + d * 10000}
        for d in range(2, 32)  # Jan 2-31
    ]


# ── BarDataDB Tests ───────────────────────────────────────────────────────────

class TestBarDataDB:
    """Tests for bar_data table CRUD operations."""

    def test_upsert_and_get_bars(self, _isolated_db, sample_bars):
        """Test basic upsert and retrieval of bar data."""
        db = _isolated_db

        # Upsert
        count = db.upsert_bars("00700.HK", sample_bars)
        assert count == 3

        # Retrieve
        bars = db.get_bars("00700.HK")
        assert len(bars) == 3
        assert bars[0]["date"] == "2026-01-02"
        assert bars[0]["open"] == 100.0
        assert bars[2]["close"] == 109.0

    def test_upsert_updates_existing(self, _isolated_db):
        """Test that upsert updates existing bars (not duplicates)."""
        db = _isolated_db

        bars_v1 = [{"date": "2026-01-02", "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000}]
        bars_v2 = [{"date": "2026-01-02", "open": 101.0, "high": 106.0, "low": 100.0, "close": 104.0, "volume": 2000}]

        db.upsert_bars("AAPL", bars_v1)
        db.upsert_bars("AAPL", bars_v2)

        bars = db.get_bars("AAPL")
        assert len(bars) == 1  # No duplicate
        assert bars[0]["open"] == 101.0  # Updated value
        assert bars[0]["volume"] == 2000

    def test_get_bars_with_date_filter(self, _isolated_db, sample_bars):
        """Test date range filtering."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)

        # Filter by start_date
        bars = db.get_bars("00700.HK", start_date="2026-01-03")
        assert len(bars) == 2
        assert bars[0]["date"] == "2026-01-03"

        # Filter by end_date
        bars = db.get_bars("00700.HK", end_date="2026-01-03")
        assert len(bars) == 2
        assert bars[1]["date"] == "2026-01-03"

        # Filter by both
        bars = db.get_bars("00700.HK", start_date="2026-01-03", end_date="2026-01-03")
        assert len(bars) == 1

    def test_get_bars_empty_symbol(self, _isolated_db):
        """Test retrieval for non-existent symbol returns empty list."""
        db = _isolated_db
        bars = db.get_bars("NONEXISTENT")
        assert bars == []

    def test_get_bars_as_dataframe(self, _isolated_db, sample_bars):
        """Test DataFrame retrieval."""
        import pandas as pd

        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)

        df = db.get_bars_as_dataframe("00700.HK")
        assert not df.empty
        assert len(df) == 3
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_get_bars_as_dataframe_empty(self, _isolated_db):
        """Test DataFrame retrieval for non-existent symbol returns empty DataFrame."""
        db = _isolated_db
        df = db.get_bars_as_dataframe("NONEXISTENT")
        assert df.empty

    def test_upsert_bars_empty_list(self, _isolated_db):
        """Test upsert with empty list returns 0."""
        db = _isolated_db
        count = db.upsert_bars("AAPL", [])
        assert count == 0

    def test_turnover_optional(self, _isolated_db):
        """Test that turnover is optional."""
        db = _isolated_db
        bars = [{"date": "2026-01-02", "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000}]
        db.upsert_bars("AAPL", bars)

        result = db.get_bars("AAPL")
        assert result[0]["turnover"] == 0.0  # Default value


# ── DataOverview Tests ────────────────────────────────────────────────────────

class TestDataOverview:
    """Tests for data_overview table (BarOverview tracking)."""

    def test_overview_created_on_upsert(self, _isolated_db, sample_bars):
        """Test that overview is automatically created/upserted."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)

        overview = db.get_overview("00700.HK")
        assert overview is not None
        assert overview["symbol"] == "00700.HK"
        assert overview["start_date"] == "2026-01-02"
        assert overview["end_date"] == "2026-01-06"
        assert overview["bar_count"] == 3

    def test_overview_range_extension(self, _isolated_db, sample_bars):
        """Test that overview extends range when new data goes beyond."""
        db = _isolated_db

        # Initial data: Jan 2-6
        db.upsert_bars("00700.HK", sample_bars)

        # Add data extending to Jan 10
        new_bars = [{"date": "2026-01-10", "open": 110.0, "high": 115.0, "low": 109.0, "close": 113.0, "volume": 800000}]
        db.upsert_bars("00700.HK", new_bars)

        overview = db.get_overview("00700.HK")
        assert overview["start_date"] == "2026-01-02"  # Unchanged
        assert overview["end_date"] == "2026-01-10"     # Extended
        assert overview["bar_count"] == 4

    def test_overview_range_backward_extension(self, _isolated_db, sample_bars):
        """Test that overview extends backward when earlier data is added."""
        db = _isolated_db

        # Initial data: Jan 2-6
        db.upsert_bars("00700.HK", sample_bars)

        # Add earlier data: Jan 1
        earlier_bars = [{"date": "2026-01-01", "open": 99.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 500000}]
        db.upsert_bars("00700.HK", earlier_bars)

        overview = db.get_overview("00700.HK")
        assert overview["start_date"] == "2026-01-01"  # Extended backward
        assert overview["end_date"] == "2026-01-06"

    def test_overview_none_for_unknown(self, _isolated_db):
        """Test that get_overview returns None for unknown symbol."""
        db = _isolated_db
        overview = db.get_overview("NONEXISTENT")
        assert overview is None

    def test_overview_source_tracking(self, _isolated_db, sample_bars):
        """Test that overview tracks data source."""
        db = _isolated_db
        db.upsert_bars("AAPL", sample_bars, source="futu")

        overview = db.get_overview("AAPL", source="futu")
        assert overview is not None
        assert overview["source"] == "futu"

        # Different source
        overview_yf = db.get_overview("AAPL", source="yfinance")
        assert overview_yf is None

    def test_get_all_overviews(self, _isolated_db, sample_bars):
        """Test retrieval of all overviews."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)
        db.upsert_bars("AAPL", sample_bars)

        overviews = db.get_all_overviews()
        assert len(overviews) == 2
        symbols = {o["symbol"] for o in overviews}
        assert symbols == {"00700.HK", "AAPL"}


# ── Missing Ranges Tests ──────────────────────────────────────────────────────

class TestMissingRanges:
    """Tests for get_missing_ranges() — incremental fetch logic."""

    def test_no_cache_returns_full_range(self, _isolated_db):
        """Test that missing range is the full requested range when no cache exists."""
        db = _isolated_db
        missing = db.get_missing_ranges("AAPL", "2026-01-01", "2026-12-31")
        assert missing == [("2026-01-01", "2026-12-31")]

    def test_fully_cached_returns_empty(self, _isolated_db, sample_bars):
        """Test that fully cached range returns empty list."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)

        # Request within cached range
        missing = db.get_missing_ranges("00700.HK", "2026-01-02", "2026-01-06")
        assert missing == []

    def test_gap_before_cached(self, _isolated_db, sample_bars):
        """Test gap detection before cached range."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)  # Jan 2-6

        # Request starts before cache
        missing = db.get_missing_ranges("00700.HK", "2026-01-01", "2026-01-06")
        assert len(missing) == 1
        assert missing[0] == ("2026-01-01", "2026-01-02")

    def test_gap_after_cached(self, _isolated_db, sample_bars):
        """Test gap detection after cached range."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)  # Jan 2-6

        # Request ends after cache
        missing = db.get_missing_ranges("00700.HK", "2026-01-02", "2026-01-10")
        assert len(missing) == 1
        assert missing[0] == ("2026-01-06", "2026-01-10")

    def test_gaps_both_sides(self, _isolated_db, sample_bars):
        """Test gap detection on both sides."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)  # Jan 2-6

        # Request extends both ways
        missing = db.get_missing_ranges("00700.HK", "2026-01-01", "2026-01-10")
        assert len(missing) == 2
        assert missing[0] == ("2026-01-01", "2026-01-02")
        assert missing[1] == ("2026-01-06", "2026-01-10")

    def test_is_cached(self, _isolated_db, sample_bars):
        """Test is_cached convenience function."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)

        assert db.is_cached("00700.HK", "2026-01-02", "2026-01-06") is True
        assert db.is_cached("00700.HK", "2026-01-01", "2026-01-06") is False
        assert db.is_cached("00700.HK", "2026-01-02", "2026-01-10") is False


# ── Batch Operations Tests ────────────────────────────────────────────────────

class TestBatchOperations:
    """Tests for bulk/batch operations."""

    def test_upsert_bars_batch(self, _isolated_db, sample_bars):
        """Test batch upsert for multiple symbols."""
        db = _isolated_db
        data = {
            "00700.HK": sample_bars,
            "AAPL": sample_bars,
        }
        total = db.upsert_bars_batch(data)
        assert total == 6  # 3 bars × 2 symbols

        assert len(db.get_bars("00700.HK")) == 3
        assert len(db.get_bars("AAPL")) == 3

    def test_clear_cache_single_symbol(self, _isolated_db, sample_bars):
        """Test clearing cache for a single symbol."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)
        db.upsert_bars("AAPL", sample_bars)

        deleted = db.clear_cache("00700.HK")
        assert deleted == 3

        assert len(db.get_bars("00700.HK")) == 0
        assert len(db.get_bars("AAPL")) == 3  # Untouched

    def test_clear_cache_all(self, _isolated_db, sample_bars):
        """Test clearing all cached data."""
        db = _isolated_db
        db.upsert_bars("00700.HK", sample_bars)
        db.upsert_bars("AAPL", sample_bars)

        deleted = db.clear_cache()
        assert deleted == 6

        assert len(db.get_bars("00700.HK")) == 0
        assert len(db.get_bars("AAPL")) == 0


# ── Migration Tests ───────────────────────────────────────────────────────────

class TestMigration:
    """Tests for migrate_json_to_sqlite module."""

    def test_load_json_universe(self, tmp_path):
        """Test loading JSON universe file."""
        from tradingagents.data.migrate_json_to_sqlite import _load_json_universe

        # Create test JSON
        test_data = [
            {"code": "AAPL", "name": "Apple", "market": "US", "type": "stock"},
            {"code": "00700.HK", "name": "腾讯", "market": "HK", "type": "stock"},
        ]
        json_path = tmp_path / "test_universe.json"
        json_path.write_text(json.dumps(test_data), encoding="utf-8")

        items = _load_json_universe(json_path)
        assert len(items) == 2
        assert items[0]["code"] == "AAPL"

    def test_load_json_universe_missing_file(self, tmp_path):
        """Test loading from non-existent file returns empty list."""
        from tradingagents.data.migrate_json_to_sqlite import _load_json_universe

        items = _load_json_universe(tmp_path / "nonexistent.json")
        assert items == []

    def test_verify_migration(self, tmp_path):
        """Test migration verification (mock scenario)."""
        from tradingagents.data.migrate_json_to_sqlite import verify_migration

        # Create test JSON
        test_data = [
            {"code": "AAPL", "name": "Apple", "market": "US", "type": "stock"},
        ]
        json_path = tmp_path / "test_universe.json"
        json_path.write_text(json.dumps(test_data), encoding="utf-8")

        # Verify (will fail if api.database not available, which is expected in test)
        result = verify_migration(json_path)
        assert result["json_count"] == 1


# ── Integration Tests ─────────────────────────────────────────────────────────

class TestIntegration:
    """Integration tests for the complete data flow."""

    def test_full_workflow(self, _isolated_db, sample_bars):
        """Test complete workflow: upsert → query → overview → missing ranges."""
        db = _isolated_db

        # Step 1: Upsert bars
        count = db.upsert_bars("00700.HK", sample_bars)
        assert count == 3

        # Step 2: Query bars
        bars = db.get_bars("00700.HK")
        assert len(bars) == 3

        # Step 3: Check overview
        overview = db.get_overview("00700.HK")
        assert overview is not None
        assert overview["start_date"] == "2026-01-02"
        assert overview["end_date"] == "2026-01-06"

        # Step 4: Check missing ranges (fully cached)
        missing = db.get_missing_ranges("00700.HK", "2026-01-02", "2026-01-06")
        assert missing == []

        # Step 5: Request extended range (needs fetch)
        missing = db.get_missing_ranges("00700.HK", "2026-01-01", "2026-01-15")
        assert len(missing) == 2  # Before and after

    def test_incremental_fetch_pattern(self, _isolated_db):
        """Test the incremental fetch pattern used by FutuProvider."""
        db = _isolated_db

        # Initial fetch: Jan 1-10
        bars_initial = [
            {"date": f"2026-01-{d:02d}", "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000}
            for d in range(1, 11)
        ]
        db.upsert_bars("AAPL", bars_initial)

        # Check what's missing for Jan 1-20
        missing = db.get_missing_ranges("AAPL", "2026-01-01", "2026-01-20")
        assert len(missing) == 1
        assert missing[0] == ("2026-01-10", "2026-01-20")

        # Fetch and cache the missing range
        bars_extended = [
            {"date": f"2026-01-{d:02d}", "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000}
            for d in range(10, 21)
        ]
        db.upsert_bars("AAPL", bars_extended)

        # Now fully cached
        assert db.is_cached("AAPL", "2026-01-01", "2026-01-20") is True

    def test_multiple_symbols_isolation(self, _isolated_db, sample_bars):
        """Test that data for different symbols is properly isolated."""
        db = _isolated_db

        db.upsert_bars("00700.HK", sample_bars)
        db.upsert_bars("AAPL", sample_bars[:2])  # Only 2 bars

        assert len(db.get_bars("00700.HK")) == 3
        assert len(db.get_bars("AAPL")) == 2
        assert len(db.get_bars("NVDA")) == 0

        overviews = db.get_all_overviews()
        assert len(overviews) == 2
