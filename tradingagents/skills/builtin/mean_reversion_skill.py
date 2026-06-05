"""Mean Reversion Skill — Contrarian range-trading strategy.

Buys when price is significantly below its moving average (oversold),
sells when price is significantly above (overbought).

Best in: rangebound/sideways markets.
Struggles in: strong trending markets (buys into falling knives).
"""

from __future__ import annotations

import logging
import math
from typing import List

from tradingagents.skills.base_skill import (
    BaseSkill,
    MarketData,
    MarketRegime,
    Signal,
    SignalDirection,
)

logger = logging.getLogger(__name__)


class MeanReversionSkill(BaseSkill):
    """Mean reversion contrarian strategy.

    Uses Bollinger Bands (20-period SMA ± 2σ) and RSI to detect
    oversold/overbought conditions:

        BUY:  Price below lower Bollinger Band AND RSI < 30
        SELL: Price above upper Bollinger Band AND RSI > 70
        HOLD: Price within bands or RSI in neutral zone.

    Confidence is proportional to the extremity of the deviation.
    """

    @property
    def name(self) -> str:
        return "mean_reversion"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Contrarian mean-reversion strategy — buys oversold, sells overbought"

    @property
    def supported_regimes(self) -> List[MarketRegime]:
        return [MarketRegime.RANGEBOUND, MarketRegime.UNKNOWN]

    def analyze(self, market_data: MarketData) -> Signal:
        """Run mean reversion analysis using Bollinger Bands and RSI.

        Args:
            market_data: Must have at least 20 prices for Bollinger Bands.

        Returns:
            Signal with direction, confidence, and band/RSI metrics.
        """
        prices = market_data.prices
        if len(prices) < 20:
            return Signal(
                direction=SignalDirection.HOLD,
                confidence=0.1,
                reason="Insufficient data for mean reversion (need 20+ bars)",
                metadata={"skill_name": self.name},
            )

        current_price = market_data.price or prices[-1]

        # ── Bollinger Bands (20-period, 2σ) ──────────────────────────────────
        sma_20 = _sma(prices, 20)
        std_20 = _std(prices[-20:])
        upper_band = sma_20 + 2 * std_20
        lower_band = sma_20 - 2 * std_20

        # Position within bands: -1 (at lower) to +1 (at upper)
        band_width = upper_band - lower_band
        if band_width > 0:
            band_position = 2 * (current_price - sma_20) / band_width
        else:
            band_position = 0.0

        # ── RSI (14-period) ──────────────────────────────────────────────────
        rsi = _rsi(prices, 14)

        # ── Scoring ──────────────────────────────────────────────────────────
        score = 0.0
        reasons: list[str] = []

        # Bollinger Band signal
        if current_price <= lower_band:
            bb_score = min(1.0, (lower_band - current_price) / (std_20 + 1e-9))
            score += bb_score * 0.5
            reasons.append(
                f"Price({current_price:.2f}) below lower BB({lower_band:.2f}) — oversold"
            )
        elif current_price >= upper_band:
            bb_score = min(1.0, (current_price - upper_band) / (std_20 + 1e-9))
            score -= bb_score * 0.5
            reasons.append(
                f"Price({current_price:.2f}) above upper BB({upper_band:.2f}) — overbought"
            )
        else:
            reasons.append(f"Price({current_price:.2f}) within BB [{lower_band:.2f}, {upper_band:.2f}]")

        # RSI signal
        if rsi < 30:
            rsi_score = (30 - rsi) / 30  # 0 at 30, 1 at 0
            score += rsi_score * 0.5
            reasons.append(f"RSI({rsi:.1f}) < 30 — oversold")
        elif rsi > 70:
            rsi_score = (rsi - 70) / 30  # 0 at 70, 1 at 100
            score -= rsi_score * 0.5
            reasons.append(f"RSI({rsi:.1f}) > 70 — overbought")
        else:
            reasons.append(f"RSI({rsi:.1f}) in neutral zone")

        # ── Direction and confidence ─────────────────────────────────────────
        confidence = min(1.0, abs(score))

        if score > 0.2:
            direction = SignalDirection.BUY
        elif score < -0.2:
            direction = SignalDirection.SELL
        else:
            direction = SignalDirection.HOLD
            confidence = max(0.1, confidence * 0.5)

        # Target: mean (SMA20) for mean-reversion trades
        target_price = sma_20 if direction != SignalDirection.HOLD else None
        stop_loss = (
            current_price * 0.97 if direction == SignalDirection.BUY
            else current_price * 1.03 if direction == SignalDirection.SELL
            else None
        )

        return Signal(
            direction=direction,
            confidence=round(confidence, 3),
            reason=" | ".join(reasons),
            target_price=target_price,
            stop_loss=stop_loss,
            metadata={
                "skill_name": self.name,
                "sma_20": round(sma_20, 4),
                "upper_band": round(upper_band, 4),
                "lower_band": round(lower_band, 4),
                "band_position": round(band_position, 4),
                "rsi": round(rsi, 2),
                "mean_reversion_score": round(score, 4),
            },
        )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sma(data: List[float], period: int) -> float:
    """Simple moving average of last `period` values."""
    if len(data) < period:
        return sum(data) / len(data) if data else 0.0
    return sum(data[-period:]) / period


def _std(data: List[float]) -> float:
    """Population standard deviation."""
    if len(data) < 2:
        return 0.0
    mean = sum(data) / len(data)
    variance = sum((x - mean) ** 2 for x in data) / len(data)
    return math.sqrt(variance)


def _rsi(prices: List[float], period: int = 14) -> float:
    """Relative Strength Index.

    Returns 0-100.  Default 50 if insufficient data.
    """
    if len(prices) < period + 1:
        return 50.0

    gains: list[float] = []
    losses: list[float] = []

    for i in range(-period, 0):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
