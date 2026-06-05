"""Value Skill — Fundamental value investing strategy.

Buys undervalued stocks (low P/E, low P/B, high dividend yield relative to
sector averages), sells overvalued ones.

Best in: bear markets (contrarian), rangebound markets.
Struggles in: strong momentum-driven bull markets where "cheap" stocks stay cheap.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from tradingagents.skills.base_skill import (
    BaseSkill,
    MarketData,
    MarketRegime,
    Signal,
    SignalDirection,
)

logger = logging.getLogger(__name__)

# ── Sector median benchmarks (approximate) ───────────────────────────────────
# These are rough global medians; in production, pull from a data provider.
SECTOR_BENCHMARKS: Dict[str, Dict[str, float]] = {
    "technology":  {"pe": 28.0, "pb": 5.0, "div_yield": 0.01},
    "finance":     {"pe": 12.0, "pb": 1.2, "div_yield": 0.03},
    "healthcare":  {"pe": 22.0, "pb": 3.5, "div_yield": 0.015},
    "consumer":    {"pe": 20.0, "pb": 3.0, "div_yield": 0.02},
    "energy":      {"pe": 10.0, "pb": 1.5, "div_yield": 0.04},
    "industrial":  {"pe": 16.0, "pb": 2.5, "div_yield": 0.025},
    "default":     {"pe": 18.0, "pb": 2.5, "div_yield": 0.02},
}


class ValueSkill(BaseSkill):
    """Fundamental value investing strategy.

    Scoring:
        - P/E ratio vs sector median: lower is better
        - P/B ratio vs sector median: lower is better
        - Dividend yield vs sector median: higher is better

    Each factor contributes a score from -1 (very overvalued) to +1 (very undervalued).
    The weighted average determines BUY/SELL/HOLD.
    """

    @property
    def name(self) -> str:
        return "value"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Fundamental value strategy — buys undervalued, sells overvalued"

    @property
    def supported_regimes(self) -> List[MarketRegime]:
        return [MarketRegime.BEAR, MarketRegime.RANGEBOUND, MarketRegime.UNKNOWN]

    def analyze(self, market_data: MarketData) -> Signal:
        """Run value analysis using fundamental ratios.

        Args:
            market_data: Must have pe_ratio, pb_ratio, dividend_yield populated.

        Returns:
            Signal with direction, confidence, and valuation metrics.
        """
        benchmarks = self._get_benchmarks(market_data.sector)
        scores: List[float] = []
        reasons: List[str] = []

        # ── P/E Ratio ────────────────────────────────────────────────────────
        if market_data.pe_ratio is not None and market_data.pe_ratio > 0:
            pe_score = self._score_ratio(
                market_data.pe_ratio, benchmarks["pe"], lower_is_better=True
            )
            scores.append(pe_score * 0.4)  # 40% weight
            reasons.append(
                f"P/E {market_data.pe_ratio:.1f} vs sector {benchmarks['pe']:.1f} "
                f"→ score {pe_score:+.2f}"
            )
        else:
            reasons.append("P/E: unavailable")

        # ── P/B Ratio ────────────────────────────────────────────────────────
        if market_data.pb_ratio is not None and market_data.pb_ratio > 0:
            pb_score = self._score_ratio(
                market_data.pb_ratio, benchmarks["pb"], lower_is_better=True
            )
            scores.append(pb_score * 0.35)  # 35% weight
            reasons.append(
                f"P/B {market_data.pb_ratio:.1f} vs sector {benchmarks['pb']:.1f} "
                f"→ score {pb_score:+.2f}"
            )
        else:
            reasons.append("P/B: unavailable")

        # ── Dividend Yield ───────────────────────────────────────────────────
        if market_data.dividend_yield is not None:
            div_score = self._score_ratio(
                market_data.dividend_yield, benchmarks["div_yield"], lower_is_better=False
            )
            scores.append(div_score * 0.25)  # 25% weight
            reasons.append(
                f"Div yield {market_data.dividend_yield:.2%} vs sector {benchmarks['div_yield']:.2%} "
                f"→ score {div_score:+.2f}"
            )
        else:
            reasons.append("Dividend yield: unavailable")

        # ── Aggregate ────────────────────────────────────────────────────────
        if not scores:
            return Signal(
                direction=SignalDirection.HOLD,
                confidence=0.1,
                reason="No fundamental data available for value analysis",
                metadata={"skill_name": self.name},
            )

        avg_score = sum(scores) / len(scores)
        confidence = min(1.0, abs(avg_score))

        if avg_score > 0.15:
            direction = SignalDirection.BUY
        elif avg_score < -0.15:
            direction = SignalDirection.SELL
        else:
            direction = SignalDirection.HOLD
            confidence = max(0.1, confidence * 0.5)

        return Signal(
            direction=direction,
            confidence=round(confidence, 3),
            reason=" | ".join(reasons),
            metadata={
                "skill_name": self.name,
                "value_score": round(avg_score, 4),
                "sector": market_data.sector or "default",
                "benchmarks": benchmarks,
            },
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _get_benchmarks(sector: str) -> Dict[str, float]:
        """Get sector benchmarks, falling back to defaults."""
        key = sector.lower().strip() if sector else "default"
        return SECTOR_BENCHMARKS.get(key, SECTOR_BENCHMARKS["default"])

    @staticmethod
    def _score_ratio(
        actual: float, benchmark: float, lower_is_better: bool
    ) -> float:
        """Score a ratio relative to benchmark.

        Returns a score from -1 to +1:
            +1: ratio is half the benchmark (very undervalued if lower_is_better)
            0:  ratio equals benchmark
            -1: ratio is double the benchmark (very overvalued if lower_is_better)
        """
        if benchmark <= 0:
            return 0.0

        ratio = actual / benchmark

        if lower_is_better:
            # Lower actual → higher score
            # ratio=0.5 → score=+1, ratio=1.0 → 0, ratio=2.0 → -1
            score = 1.0 - ratio
        else:
            # Higher actual → higher score
            # ratio=2.0 → +1, ratio=1.0 → 0, ratio=0.5 → -1
            score = ratio - 1.0

        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, score))
