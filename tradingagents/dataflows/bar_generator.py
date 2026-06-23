"""
BarGenerator — Real-time K-line aggregation for TradingAgents-Futu.

Generates higher-timeframe bars from 1-minute bars or tick data.
Based on vnpy/trader/utility.py BarGenerator, adapted for TAF.

Phase 13.1: BarGenerator K-line synthesis

Usage:
    from tradingagents.dataflows.bar_generator import BarGenerator

    # Generate 5-minute bars from 1-minute bars
    def on_5min_bar(bar: BarData):
        print(f"5m bar: {bar.symbol} O={bar.open_price} C={bar.close_price}")

    gen = BarGenerator(on_bar=on_5min_bar, window=5, interval="5m")
    for bar in bars_1m:
        gen.update_bar(bar)

    # Generate 1-minute bars from tick data
    def on_1m_bar(bar: BarData):
        print(f"1m bar: {bar.symbol} O={bar.open_price} C={bar.close_price}")

    gen = BarGenerator(on_bar=on_1m_bar)
    for tick in ticks:
        gen.update_tick(tick)
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Callable, Optional

from tradingagents.models.data_objects import BarData, TickData


class BarGenerator:
    """Real-time K-line bar aggregation.

    Supports:
    1. Tick → 1-minute bar aggregation (update_tick)
    2. 1-minute → X-minute bar aggregation (update_bar)
    3. 1-minute → X-hour bar aggregation (update_bar with interval="1h")
    4. 1-minute → daily bar aggregation (update_bar with interval="1d")

    For minute bars: window must divide 60 (2, 3, 5, 6, 10, 15, 20, 30)
    For hour bars: window can be any number
    """

    def __init__(
        self,
        on_bar: Callable[[BarData], None],
        window: int = 0,
        interval: str = "1m",
        daily_end: Optional[time] = None,
    ):
        """Initialize BarGenerator.

        Args:
            on_bar: Callback when a completed bar is generated.
            window: Number of intervals to aggregate (e.g., 5 for 5-minute bars).
            interval: Target interval ("1m", "5m", "15m", "1h", "4h", "1d").
            daily_end: Required for daily bars. The daily close time (e.g., time(16, 0)).
        """
        self.on_bar = on_bar
        self.window = window
        self.interval = interval
        self.daily_end = daily_end

        # Current 1-minute bar being built from ticks
        self.bar: Optional[BarData] = None
        self.last_tick: Optional[TickData] = None

        # Window bar being built from 1-minute bars
        self.window_bar: Optional[BarData] = None

        # Hour bar being built from 1-minute bars
        self.hour_bar: Optional[BarData] = None

        # Daily bar being built
        self.daily_bar: Optional[BarData] = None

    def update_tick(self, tick: TickData) -> None:
        """Update with new tick data, generating 1-minute bars.

        Args:
            tick: New tick data from WebSocket or market feed.
        """
        new_minute = False

        # Filter zero price
        if not tick.last_price:
            return

        # Check if we need a new 1-minute bar
        if not self.bar:
            new_minute = True
        elif (
            self.bar.datetime
            and tick.datetime
            and (
                self.bar.datetime.minute != tick.datetime.minute
                or self.bar.datetime.hour != tick.datetime.hour
            )
        ):
            # Previous minute is complete, emit it
            self.bar.datetime = self.bar.datetime.replace(second=0, microsecond=0)
            self.on_bar(self.bar)
            new_minute = True

        if new_minute:
            self.bar = BarData(
                symbol=tick.symbol,
                datetime=tick.datetime,
                interval="1m",
                open_price=tick.last_price,
                high_price=tick.last_price,
                low_price=tick.last_price,
                close_price=tick.last_price,
            )
        elif self.bar:
            # Update high/low/close
            self.bar.high_price = max(self.bar.high_price, tick.last_price)
            self.bar.low_price = min(self.bar.low_price, tick.last_price)
            self.bar.close_price = tick.last_price

        # Update volume/turnover from tick changes
        if self.last_tick and self.bar:
            volume_change = tick.volume - self.last_tick.volume
            self.bar.volume += max(volume_change, 0)

            turnover_change = tick.turnover - self.last_tick.turnover
            self.bar.turnover += max(turnover_change, 0)

        self.last_tick = tick

    def update_bar(self, bar: BarData) -> None:
        """Update with new 1-minute bar, generating higher-timeframe bars.

        Args:
            bar: 1-minute bar data (from update_tick or direct input).
        """
        if self.interval in ("5m", "15m", "30m"):
            self._update_minute_window(bar)
        elif self.interval in ("1h", "4h"):
            self._update_hour_window(bar)
        elif self.interval == "1d":
            self._update_daily_window(bar)
        else:
            # Default: just pass through
            self.on_bar(bar)

    def _update_minute_window(self, bar: BarData) -> None:
        """Aggregate 1-minute bars into X-minute bars."""
        # Initialize window bar if needed
        if not self.window_bar:
            dt = bar.datetime.replace(second=0, microsecond=0) if bar.datetime else None
            self.window_bar = BarData(
                symbol=bar.symbol,
                datetime=dt,
                interval=self.interval,
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                close_price=bar.close_price,
                volume=bar.volume,
                turnover=bar.turnover,
            )
            return

        # Update high/low
        self.window_bar.high_price = max(self.window_bar.high_price, bar.high_price)
        self.window_bar.low_price = min(self.window_bar.low_price, bar.low_price)

        # Update close/volume/turnover
        self.window_bar.close_price = bar.close_price
        self.window_bar.volume += bar.volume
        self.window_bar.turnover += bar.turnover

        # Check if window is complete
        if bar.datetime and not (bar.datetime.minute + 1) % self.window:
            self.on_bar(self.window_bar)
            self.window_bar = None

    def _update_hour_window(self, bar: BarData) -> None:
        """Aggregate 1-minute bars into X-hour bars."""
        # Initialize hour bar if needed
        if not self.hour_bar:
            dt = bar.datetime.replace(minute=0, second=0, microsecond=0) if bar.datetime else None
            self.hour_bar = BarData(
                symbol=bar.symbol,
                datetime=dt,
                interval=self.interval,
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                close_price=bar.close_price,
                volume=bar.volume,
                turnover=bar.turnover,
            )
            return

        finished_bar = None

        # If minute is 59, complete the hour bar
        if bar.datetime and bar.datetime.minute == 59:
            self.hour_bar.high_price = max(self.hour_bar.high_price, bar.high_price)
            self.hour_bar.low_price = min(self.hour_bar.low_price, bar.low_price)
            self.hour_bar.close_price = bar.close_price
            self.hour_bar.volume += bar.volume
            self.hour_bar.turnover += bar.turnover

            finished_bar = self.hour_bar
            self.hour_bar = None

        # If new hour started, emit previous hour bar
        elif bar.datetime and self.hour_bar.datetime and bar.datetime.hour != self.hour_bar.datetime.hour:
            finished_bar = self.hour_bar

            dt = bar.datetime.replace(minute=0, second=0, microsecond=0)
            self.hour_bar = BarData(
                symbol=bar.symbol,
                datetime=dt,
                interval=self.interval,
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                close_price=bar.close_price,
                volume=bar.volume,
                turnover=bar.turnover,
            )
        else:
            # Still in same hour, update
            self.hour_bar.high_price = max(self.hour_bar.high_price, bar.high_price)
            self.hour_bar.low_price = min(self.hour_bar.low_price, bar.low_price)
            self.hour_bar.close_price = bar.close_price
            self.hour_bar.volume += bar.volume
            self.hour_bar.turnover += bar.turnover

        # Emit finished bar
        if finished_bar:
            self.on_bar(finished_bar)

    def _update_daily_window(self, bar: BarData) -> None:
        """Aggregate 1-minute bars into daily bars."""
        if not self.daily_bar:
            dt = bar.datetime.replace(hour=0, minute=0, second=0, microsecond=0) if bar.datetime else None
            self.daily_bar = BarData(
                symbol=bar.symbol,
                datetime=dt,
                interval="1d",
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                close_price=bar.close_price,
                volume=bar.volume,
                turnover=bar.turnover,
            )
            return

        # Update high/low/close/volume
        self.daily_bar.high_price = max(self.daily_bar.high_price, bar.high_price)
        self.daily_bar.low_price = min(self.daily_bar.low_price, bar.low_price)
        self.daily_bar.close_price = bar.close_price
        self.daily_bar.volume += bar.volume
        self.daily_bar.turnover += bar.turnover

        # Check if daily bar is complete (at daily_end time)
        if self.daily_end and bar.datetime:
            bar_time = bar.datetime.time()
            if bar_time >= self.daily_end:
                self.on_bar(self.daily_bar)
                self.daily_bar = None

    def generate_from_bars(self, bars: list[BarData], target_interval: str) -> list[BarData]:
        """Batch convert 1-minute bars to higher timeframe.

        Args:
            bars: List of 1-minute bars (must be sorted by datetime).
            target_interval: Target interval ("5m", "15m", "1h", "4h", "1d").

        Returns:
            List of aggregated bars at target interval.
        """
        result = []

        def on_bar(bar: BarData):
            result.append(bar)

        interval_window = {
            "5m": 5, "15m": 15, "30m": 30,
            "1h": 1, "4h": 4, "1d": 1,
        }
        window = interval_window.get(target_interval, 1)

        gen = BarGenerator(on_bar=on_bar, window=window, interval=target_interval)

        for bar in bars:
            gen.update_bar(bar)

        # Flush remaining bar
        if gen.window_bar:
            result.append(gen.window_bar)
        if gen.hour_bar:
            result.append(gen.hour_bar)
        if gen.daily_bar:
            result.append(gen.daily_bar)

        return result


# ── Convenience functions ────────────────────────────────────────────────────

def resample_bars(bars: list[BarData], target_interval: str) -> list[BarData]:
    """Resample bars to a higher timeframe.

    Args:
        bars: Input bars (any interval, but 1m is recommended).
        target_interval: Target interval ("5m", "15m", "1h", "4h", "1d").

    Returns:
        List of aggregated bars.

    Example:
        bars_5m = resample_bars(bars_1m, "5m")
        bars_1h = resample_bars(bars_1m, "1h")
    """
    # Map interval to window size
    interval_window = {
        "5m": 5, "15m": 15, "30m": 30,
        "1h": 1, "4h": 4, "1d": 1,
    }
    window = interval_window.get(target_interval, 1)

    gen = BarGenerator(on_bar=lambda _: None, window=window, interval=target_interval)
    return gen.generate_from_bars(bars, target_interval)
