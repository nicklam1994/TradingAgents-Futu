"""Tests for FutuProvider enhanced features: cache and OHLC validation."""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock

# Import the module under test
from tradingagents.dataflows.providers.futu_provider import FutuProvider, _PanelCache


class TestPanelCache:
    """Tests for the _PanelCache class."""

    def setup_method(self):
        """Create fresh cache for each test."""
        self.cache = _PanelCache()

    def test_cache_miss_on_empty(self):
        """Cache should return None when empty."""
        result = self.cache.get(("AAPL",), "2024-01-01", "2024-12-31", "qfq")
        assert result is None

    def test_cache_hit_same_day(self):
        """Cache should return stored value on same day."""
        symbols = ("AAPL", "NVDA")
        start = "2024-01-01"
        end = "2024-12-31"
        autype = "qfq"

        # Create mock panel data
        panel = {
            "open": pd.DataFrame({"AAPL": [150.0], "NVDA": [500.0]},
                                index=pd.DatetimeIndex(["2024-01-01"])),
            "close": pd.DataFrame({"AAPL": [155.0], "NVDA": [510.0]},
                                index=pd.DatetimeIndex(["2024-01-01"])),
        }

        # Store and retrieve
        self.cache.put(symbols, start, end, autype, panel)
        result = self.cache.get(symbols, start, end, autype)

        assert result is not None
        assert "open" in result
        assert result["open"].iloc[0]["AAPL"] == 150.0

    def test_cache_miss_different_symbols(self):
        """Cache should miss when symbols differ."""
        panel = {"open": pd.DataFrame()}

        self.cache.put(("AAPL",), "2024-01-01", "2024-12-31", "qfq", panel)
        result = self.cache.get(("NVDA",), "2024-01-01", "2024-12-31", "qfq")

        assert result is None

    def test_cache_miss_different_dates(self):
        """Cache should miss when date range differs."""
        panel = {"open": pd.DataFrame()}

        self.cache.put(("AAPL",), "2024-01-01", "2024-06-30", "qfq", panel)
        result = self.cache.get(("AAPL",), "2024-01-01", "2024-12-31", "qfq")

        assert result is None

    def test_cache_miss_different_autype(self):
        """Cache should miss when adjustment type differs."""
        panel = {"open": pd.DataFrame()}

        self.cache.put(("AAPL",), "2024-01-01", "2024-12-31", "qfq", panel)
        result = self.cache.get(("AAPL",), "2024-01-01", "2024-12-31", "hfq")

        assert result is None

    def test_cache_expiry_cross_day(self):
        """Cache should expire when crossing trading day boundary."""
        symbols = ("AAPL",)
        panel = {"open": pd.DataFrame()}

        # Mock date to simulate next day
        with patch.object(self.cache, '_get_cache_date') as mock_date:
            # Store on "day 1"
            mock_date.return_value = date(2024, 1, 1)
            self.cache.put(symbols, "2024-01-01", "2024-12-31", "qfq", panel)

            # Retrieve on "day 2" — should miss
            mock_date.return_value = date(2024, 1, 2)
            result = self.cache.get(symbols, "2024-01-01", "2024-12-31", "qfq")

        assert result is None

    def test_cache_clear(self):
        """Cache should be empty after clear()."""
        panel = {"open": pd.DataFrame()}
        self.cache.put(("AAPL",), "2024-01-01", "2024-12-31", "qfq", panel)

        self.cache.clear()
        result = self.cache.get(("AAPL",), "2024-01-01", "2024-12-31", "qfq")

        assert result is None

    def test_cache_symbol_order_independent(self):
        """Cache should be independent of symbol list order."""
        panel = {"open": pd.DataFrame()}
        self.cache.put(("AAPL", "NVDA"), "2024-01-01", "2024-12-31", "qfq", panel)

        # Different order should still hit (frozenset)
        result = self.cache.get(("NVDA", "AAPL"), "2024-01-01", "2024-12-31", "qfq")
        assert result is not None


class TestValidateOHLC:
    """Tests for FutuProvider._validate_ohlc() method."""

    def _make_df(self, data: dict, start="2024-01-01") -> pd.DataFrame:
        """Helper to create DataFrame with DatetimeIndex."""
        n = len(next(iter(data.values())))
        dates = pd.date_range(start, periods=n, freq="D")
        return pd.DataFrame(data, index=dates)

    def test_valid_data(self):
        """Clean data should pass validation."""
        df = self._make_df({
            "open":   [100.0, 101.0, 102.0],
            "high":   [105.0, 106.0, 107.0],
            "low":    [99.0, 100.0, 101.0],
            "close":  [103.0, 104.0, 105.0],
            "volume": [1000, 1100, 1200],
        })

        result = FutuProvider._validate_ohlc(df)

        assert result["is_valid"] is True
        assert result["violations"] == {}
        assert result["stats"]["total_bars"] == 3
        assert result["stats"]["valid_bars"] == 3

    def test_nan_values(self):
        """NaN values should be detected."""
        df = self._make_df({
            "open":   [100.0, np.nan, 102.0],
            "high":   [105.0, 106.0, 107.0],
            "low":    [99.0, 100.0, 101.0],
            "close":  [103.0, 104.0, 105.0],
            "volume": [1000, 1100, 1200],
        })

        result = FutuProvider._validate_ohlc(df, strategy="warn")

        assert result["is_valid"] is False
        assert "nan_values" in result["violations"]
        assert len(result["violations"]["nan_values"]) == 1
        assert result["stats"]["nan_count"] == 1

    def test_invalid_price_close_zero(self):
        """Close price <= 0 should be detected."""
        df = self._make_df({
            "open":   [100.0, 101.0],
            "high":   [105.0, 106.0],
            "low":    [99.0, 100.0],
            "close":  [0.0, 104.0],  # Invalid: close = 0
            "volume": [1000, 1100],
        })

        result = FutuProvider._validate_ohlc(df, strategy="warn")

        assert result["is_valid"] is False
        assert "invalid_prices" in result["violations"]
        assert len(result["violations"]["invalid_prices"]) == 1

    def test_invalid_price_high_less_than_low(self):
        """High < low should be detected."""
        df = self._make_df({
            "open":   [100.0, 101.0],
            "high":   [95.0, 106.0],   # Invalid: high < low
            "low":    [99.0, 100.0],
            "close":  [103.0, 104.0],
            "volume": [1000, 1100],
        })

        result = FutuProvider._validate_ohlc(df, strategy="warn")

        assert result["is_valid"] is False
        assert "invalid_prices" in result["violations"]
        assert len(result["violations"]["invalid_prices"]) == 1

    def test_zero_volume(self):
        """Zero volume bars should be detected."""
        df = self._make_df({
            "open":   [100.0, 101.0, 102.0],
            "high":   [105.0, 106.0, 107.0],
            "low":    [99.0, 100.0, 101.0],
            "close":  [103.0, 104.0, 105.0],
            "volume": [1000, 0, 1200],  # Zero volume
        })

        result = FutuProvider._validate_ohlc(df, strategy="warn")

        assert result["is_valid"] is False
        assert "zero_volume" in result["violations"]
        assert len(result["violations"]["zero_volume"]) == 1

    def test_price_jump(self):
        """Large price jumps should be detected."""
        df = self._make_df({
            "open":   [100.0, 200.0, 201.0],  # 100% jump
            "high":   [105.0, 210.0, 211.0],
            "low":    [99.0, 195.0, 199.0],
            "close":  [103.0, 205.0, 203.0],
            "volume": [1000, 1100, 1200],
        })

        result = FutuProvider._validate_ohlc(df, strategy="warn", price_jump_threshold=0.5)

        assert result["is_valid"] is False
        assert "price_jumps" in result["violations"]
        assert len(result["violations"]["price_jumps"]) == 1

    def test_drop_strategy(self):
        """Drop strategy should remove invalid rows."""
        df = self._make_df({
            "open":   [100.0, np.nan, 102.0],
            "high":   [105.0, 106.0, 107.0],
            "low":    [99.0, 100.0, 101.0],
            "close":  [103.0, 104.0, 105.0],
            "volume": [1000, 1100, 1200],
        })

        result = FutuProvider._validate_ohlc(df, strategy="drop")

        assert result["is_valid"] is False
        assert len(result["cleaned_df"]) == 2  # Removed 1 NaN row
        assert result["stats"]["dropped_bars"] == 1

    def test_raise_strategy(self):
        """Raise strategy should raise ValueError on violations."""
        df = self._make_df({
            "open":   [100.0, 101.0],
            "high":   [95.0, 106.0],   # Invalid
            "low":    [99.0, 100.0],
            "close":  [103.0, 104.0],
            "volume": [1000, 1100],
        })

        with pytest.raises(ValueError, match="OHLC validation failed"):
            FutuProvider._validate_ohlc(df, strategy="raise")

    def test_empty_dataframe(self):
        """Empty DataFrame should be handled gracefully."""
        df = pd.DataFrame()

        result = FutuProvider._validate_ohlc(df)

        assert result["is_valid"] is True
        assert result["stats"]["total_bars"] == 0

    def test_missing_columns(self):
        """DataFrame missing OHLC columns should be handled gracefully."""
        df = pd.DataFrame({"price": [100.0, 101.0]})

        result = FutuProvider._validate_ohlc(df)

        assert result["is_valid"] is True

    def test_repair_suggestions(self):
        """Repair suggestions should be provided for each violation type."""
        df = self._make_df({
            "open":   [100.0, np.nan, 200.0],
            "high":   [95.0, 106.0, 210.0],     # Invalid: high < low on row 0
            "low":    [99.0, 100.0, 195.0],
            "close":  [103.0, 104.0, 205.0],
            "volume": [0, 1100, 1200],            # Zero volume
        })

        result = FutuProvider._validate_ohlc(df, strategy="warn")

        assert len(result["repair_suggestions"]) >= 3  # NaN, invalid price, zero volume


class TestGetPanelDataCache:
    """Integration tests for get_panel_data() caching."""

    @patch('tradingagents.dataflows.providers.futu_provider._panel_cache')
    def test_cache_hit_returns_cached(self, mock_cache):
        """Should return cached data when cache hits."""
        mock_cache.get.return_value = {"open": pd.DataFrame()}

        provider = FutuProvider()
        result = provider.get_panel_data(["AAPL"], "2024-01-01", "2024-12-31")

        assert result == {"open": pd.DataFrame()}
        mock_cache.get.assert_called_once()

    @patch('tradingagents.dataflows.providers.futu_provider._panel_cache')
    def test_cache_miss_fetches_and_stores(self, mock_cache):
        """Should fetch from API and store in cache on miss."""
        mock_cache.get.return_value = None  # Cache miss

        # Mock the Futu API call
        mock_ctx = MagicMock()
        mock_df = pd.DataFrame({
            "time_key": ["2024-01-01", "2024-01-02"],
            "open": [100.0, 101.0],
            "high": [105.0, 106.0],
            "low": [99.0, 100.0],
            "close": [103.0, 104.0],
            "volume": [1000, 1100],
        })
        mock_ctx.request_history_kline.return_value = (0, mock_df, None)

        with patch('tradingagents.dataflows.providers.futu_provider.FutuProvider._get_quote_ctx', return_value=mock_ctx):
            provider = FutuProvider()
            result = provider.get_panel_data(["AAPL"], "2024-01-01", "2024-01-02")

        # Should have stored in cache
        mock_cache.put.assert_called_once()
        assert "open" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
