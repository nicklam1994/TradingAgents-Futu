"""Tests for W2 (real entry price) and W3 (stale position) in Observer."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from tradingagents.orchestrator.observer import (
    AlertType,
    Observer,
    PositionAlert,
    PositionSnapshot,
)


class TestW2EntryPriceResolution:
    """W2: Real entry price from history orders."""

    def test_resolve_entry_price_from_history(self):
        """Should pick earliest BUY order's dealt_avg_price."""
        mock_orders = [
            {"order_type": "BUY", "dealt_avg_price": 105.0, "create_time": "2026-02-01"},
            {"order_type": "BUY", "dealt_avg_price": 100.5, "create_time": "2026-01-01"},
            {"order_type": "SELL", "dealt_avg_price": 110.0, "create_time": "2026-03-01"},
        ]
        mock_fn = MagicMock(return_value=mock_orders)
        o = Observer(get_history_orders=mock_fn)

        pos_data = {"symbol": "HK.00700", "entry_price": 999.0, "current_price": 105.0, "quantity": 100, "side": "long"}
        resolved = o._resolve_entry_price(pos_data)

        assert resolved == 100.5, f"Expected 100.5 (earliest BUY), got {resolved}"
        assert o._entry_prices["HK.00700"]["price"] == 100.5
        mock_fn.assert_called_once_with(symbol="HK.00700", status_filter=["FILLED_ALL"])

    def test_resolve_entry_price_cache(self):
        """Second call should hit cache, not call the provider again."""
        mock_fn = MagicMock(return_value=[
            {"order_type": "BUY", "dealt_avg_price": 100.0, "create_time": "2026-01-01"},
        ])
        o = Observer(get_history_orders=mock_fn)

        pos = {"symbol": "HK.09988"}
        r1 = o._resolve_entry_price(pos)
        r2 = o._resolve_entry_price(pos)

        assert r1 == 100.0
        assert r2 == 100.0
        assert mock_fn.call_count == 1, "Should only call provider once (cached on second)"

    def test_resolve_no_buy_orders(self):
        """Return None if only SELL orders found (fallback to cost_price)."""
        mock_fn = MagicMock(return_value=[
            {"order_type": "SELL", "dealt_avg_price": 50.0, "create_time": "2026-01-01"},
        ])
        o = Observer(get_history_orders=mock_fn)

        r = o._resolve_entry_price({"symbol": "HK.00001"})
        assert r is None

    def test_resolve_no_orders(self):
        """Return None if no history orders at all."""
        mock_fn = MagicMock(return_value=[])
        o = Observer(get_history_orders=mock_fn)

        r = o._resolve_entry_price({"symbol": "HK.00001"})
        assert r is None

    def test_resolve_no_provider(self):
        """Return None if no get_history_orders callable configured."""
        o = Observer()
        r = o._resolve_entry_price({"symbol": "HK.00001"})
        assert r is None

    def test_resolve_provider_exception(self):
        """Return None if provider throws — graceful fallback."""
        mock_fn = MagicMock(side_effect=RuntimeError("API down"))
        o = Observer(get_history_orders=mock_fn)

        r = o._resolve_entry_price({"symbol": "HK.00001"})
        assert r is None

    def test_resolve_zero_price(self):
        """Return None if dealt_avg_price is 0 (invalid)."""
        mock_fn = MagicMock(return_value=[
            {"order_type": "BUY", "dealt_avg_price": 0, "create_time": "2026-01-01"},
        ])
        o = Observer(get_history_orders=mock_fn)

        r = o._resolve_entry_price({"symbol": "HK.00001"})
        assert r is None

    def test_check_positions_uses_resolved_price(self):
        """Integration: check_positions should use resolved entry_price for P&L."""
        mock_fn = MagicMock(return_value=[
            {"order_type": "BUY", "dealt_avg_price": 100.0, "create_time": "2026-01-01"},
        ])
        o = Observer(stop_loss_pct=-0.05, get_history_orders=mock_fn)

        positions = [
            {"symbol": "HK.00700", "entry_price": 999.0, "current_price": 105.0, "quantity": 100, "side": "long"},
        ]
        alerts = o.check_positions(positions)

        # Entry price should have been resolved to 100.0 (not 999.0)
        # With current_price=105.0 and entry_price=100.0, P&L = +5% (not a stop-loss)
        assert o._entry_prices["HK.00700"]["price"] == 100.0


class TestW3StalePosition:
    """W3: Stale position exit with max_holding_days."""

    def test_max_holding_days_default(self):
        """Default max_holding_days should be 30."""
        o = Observer()
        assert o._max_holding_days == 30

    def test_max_holding_days_custom(self):
        """Should accept custom max_holding_days."""
        o = Observer(max_holding_days=15)
        assert o._max_holding_days == 15

    def test_first_seen_tracking(self):
        """First call should record first_seen, subsequent calls should not update."""
        o = Observer()
        pos = [{"symbol": "HK.00700", "entry_price": 100.0, "current_price": 105.0, "quantity": 100, "side": "long"}]

        o.check_positions(pos)
        first = o._first_seen.get("HK.00700")
        assert first is not None

        import time
        time.sleep(0.01)
        o.check_positions(pos)
        second = o._first_seen.get("HK.00700")
        assert first == second, "first_seen should not update on subsequent calls"

    def test_stale_alert_triggered(self):
        """Should trigger STALE_POSITION alert when holding_days >= max_holding_days."""
        o = Observer(max_holding_days=1, get_history_orders=None)

        # Manually set first_seen to 2 days ago
        o._first_seen["HK.00700"] = datetime.now(timezone.utc) - timedelta(days=2)

        positions = [
            {"symbol": "HK.00700", "entry_price": 100.0, "current_price": 105.0, "quantity": 100, "side": "long"},
        ]
        alerts = o.check_positions(positions)

        stale_alerts = [a for a in alerts if a.alert_type == AlertType.STALE_POSITION]
        assert len(stale_alerts) == 1, f"Expected 1 stale alert, got {len(stale_alerts)}"

        alert = stale_alerts[0]
        assert alert.symbol == "HK.00700"
        assert alert.should_exit is True
        assert alert.severity == "warning"
        assert "holding_days" in alert.metadata
        assert alert.metadata["holding_days"] >= 2
        assert alert.metadata["max_holding_days"] == 1
        assert "first_seen_at" in alert.metadata

    def test_no_stale_alert_within_limit(self):
        """Should NOT trigger stale alert if within max_holding_days."""
        o = Observer(max_holding_days=30)

        # Set first_seen to just now
        o._first_seen["HK.00700"] = datetime.now(timezone.utc)

        positions = [
            {"symbol": "HK.00700", "entry_price": 100.0, "current_price": 105.0, "quantity": 100, "side": "long"},
        ]
        alerts = o.check_positions(positions)

        stale_alerts = [a for a in alerts if a.alert_type == AlertType.STALE_POSITION]
        assert len(stale_alerts) == 0, "Should not trigger stale alert within limit"

    def test_stale_alert_includes_entry_and_current_price(self):
        """STALE_POSITION alert should contain entry_price, current_price, holding_days."""
        o = Observer(max_holding_days=1)
        o._first_seen["HK.00700"] = datetime.now(timezone.utc) - timedelta(days=5)

        positions = [
            {"symbol": "HK.00700", "entry_price": 100.0, "current_price": 90.0, "quantity": 100, "side": "long"},
        ]
        alerts = o.check_positions(positions)

        stale = [a for a in alerts if a.alert_type == AlertType.STALE_POSITION][0]
        assert stale.entry_price == 100.0
        assert stale.current_price == 90.0
        assert stale.metadata["holding_days"] >= 5
        assert stale.metadata["max_holding_days"] == 1

    def test_stale_alert_message_content(self):
        """Alert message should contain symbol, days, entry, current, P&L."""
        o = Observer(max_holding_days=7)
        o._first_seen["HK.09988"] = datetime.now(timezone.utc) - timedelta(days=10)

        positions = [
            {"symbol": "HK.09988", "entry_price": 50.0, "current_price": 55.0, "quantity": 200, "side": "long"},
        ]
        alerts = o.check_positions(positions)

        stale = [a for a in alerts if a.alert_type == AlertType.STALE_POSITION][0]
        msg = stale.message
        assert "HK.09988" in msg
        assert "STALE POSITION" in msg
        assert "50.0000" in msg
        assert "55.0000" in msg


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
