"""Momentum Skill — Trend-following strategy.

Buys when price shows upward momentum (price above SMA, recent gains),
sells when momentum fades (price below SMA, recent losses).

Best in: bull markets and strong trends.
Struggles in: rangebound/choppy markets.
"""

from __future__ import annotations

import logging
from typing import List

from tradingagents.skills.base_skill import (
    BaseSkill,
    MarketData,
    MarketRegime,
    Signal,
    SignalDirection,
)

logger = logging.getLogger(__name__)


class MomentumSkill(BaseSkill):
    """Momentum-based trend-following strategy.

    Signals:
        BUY:  Price above 20-period SMA AND 10-period SMA > 20-period SMA
              AND recent 5-period return > 0.
        SELL: Price below 20-period SMA AND 10-period SMA < 20-period SMA
              AND recent 5-period return < 0.
        HOLD: Mixed or insufficient data.

    Confidence is proportional to the strength of the trend (distance from SMA,
    SMA crossover magnitude, recent return magnitude).
    """

    @property
    def name(self) -> str:
        return "momentum"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Trend-following momentum strategy — buys strength, sells weakness"

    @property
    def supported_regimes(self) -> List[MarketRegime]:
        return [MarketRegime.BULL, MarketRegime.UNKNOWN]

    def analyze(self, market_data: MarketData) -> Signal:
        """Run momentum analysis on price data.

        Args:
            market_data: Must have at least 20 prices for SMA calculation.

        Returns:
            Signal with direction, confidence, and momentum metrics.
        """
        prices = market_data.prices
        if len(prices) < 20:
            return Signal(
                direction=SignalDirection.HOLD,
                confidence=0.1,
                reason="Insufficient price data for momentum analysis (need 20+ bars)",
            )

        current_price = market_data.price or prices[-1]

        # Calculate SMAs
        sma_10 = _sma(prices, 10)
        sma_20 = _sma(prices, 20)

        # Recent return (5-period)
        if len(prices) >= 6:
            recent_return = (prices[-1] - prices[-6]) / prices[-6]
        else:
            recent_return = 0.0

        # Price position relative to SMAs
        price_above_sma20 = current_price > sma_20
        sma_crossover = sma_10 > sma_20  # Golden cross signal

        # Momentum score: combine price position + SMA cross + recent return
        score = 0.0
        reasons: list[str] = []

        # SMA crossover (strongest signal)
        if sma_crossover:
            score += 0.4
            reasons.append(f"SMA10({sma_10:.2f}) > SMA20({sma_20:.2f}) — bullish crossover")
        else:
            score -= 0.4
            reasons.append(f"SMA10({sma_10:.2f}) < SMA20({sma_20:.2f}) — bearish crossover")

        # Price above/below SMA20
        if price_above_sma20:
            score += 0.3
            reasons.append(f"Price({current_price:.2f}) above SMA20({sma_20:.2f})")
        else:
            score -= 0.3
            reasons.append(f"Price({current_price:.2f}) below SMA20({sma_20:.2f})")

        # Recent return
        if recent_return > 0:
            score += 0.3
            reasons.append(f"Recent 5-bar return: {recent_return:+.2%}")
        else:
            score -= 0.3
            reasons.append(f"Recent 5-bar return: {recent_return:+.2%}")

        # Determine direction and confidence
        confidence = min(1.0, abs(score))
        if score > 0.2:
            direction = SignalDirection.BUY
        elif score < -0.2:
            direction = SignalDirection.SELL
        else:
            direction = SignalDirection.HOLD
            confidence = max(0.1, confidence * 0.5)  # Reduce confidence for HOLD

        # Price targets
        atr = _atr(prices, 14) if len(prices) >= 14 else abs(current_price * 0.02)
        target_price = current_price + atr * 2 if direction == SignalDirection.BUY else None
        stop_loss = current_price - atr * 1.5 if direction == SignalDirection.BUY else None

        return Signal(
            direction=direction,
            confidence=round(confidence, 3),
            reason=" | ".join(reasons),
            target_price=target_price,
            stop_loss=stop_loss,
            metadata={
                "skill_name": self.name,
                "sma_10": round(sma_10, 4),
                "sma_20": round(sma_20, 4),
                "recent_return": round(recent_return, 4),
                "momentum_score": round(score, 4),
                "atr": round(atr, 4) if atr else None,
            },
        )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sma(data: List[float], period: int) -> float:
    """Simple moving average of last `period` values."""
    if len(data) < period:
        return sum(data) / len(data)
    return sum(data[-period:]) / period


def _atr(prices: List[float], period: int = 14) -> float:
    """Approximate ATR using close-to-close range (no high/low available).

    This is a simplified version — True ATR uses high/low/close.
    We use absolute close-to-close changes as a proxy.
    """
    if len(prices) < period + 1:
        period = len(prices) - 1
    if period < 1:
        return abs(prices[-1] * 0.02) if prices else 0.0

    changes = [abs(prices[i] - prices[i - 1]) for i in range(-period, 0)]
    return sum(changes) / len(changes)
