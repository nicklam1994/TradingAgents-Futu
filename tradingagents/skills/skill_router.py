"""Skill Router — Market state → strategy selection.

Routes analysis requests to the most appropriate strategy plugins
based on the current market regime (bull/bear/rangebound).

Usage:
    router = SkillRouter(registry)
    selected = router.route(market_data)        # returns List[BaseSkill]
    signals = router.analyze(market_data)        # runs analysis on selected skills
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
from tradingagents.skills.skill_registry import SkillRegistry

logger = logging.getLogger(__name__)


# ── Regime → skill preference weights ───────────────────────────────────────

# When multiple skills support the same regime, prefer those that are
# specialized (fewer supported regimes = more specialized).
REGIME_PREFERENCE: Dict[MarketRegime, List[str]] = {
    MarketRegime.BULL: [
        "momentum",      # Trend-following excels in bull markets
        "value",         # Value can find undervalued stocks even in bulls
        "mean_reversion", # Less useful in strong trends
    ],
    MarketRegime.BEAR: [
        "value",          # Value shines in bear markets (contrarian)
        "mean_reversion", # Mean reversion catches oversold bounces
        "momentum",       # Momentum underperforms in bears
    ],
    MarketRegime.RANGEBOUND: [
        "mean_reversion", # Perfect for range-bound markets
        "value",          # Value works in sideways markets
        "momentum",       # Momentum struggles in ranges
    ],
    MarketRegime.UNKNOWN: [
        "value",          # Default: conservative value approach
        "mean_reversion",
        "momentum",
    ],
}


class SkillRouter:
    """Routes market data to the best-fit strategy skills.

    Selection logic:
        1. Determine market regime from market_data (or use explicit regime).
        2. Filter registry to skills that support this regime.
        3. Rank by specialization (fewer supported regimes = more specialized).
        4. Optionally limit to top-N skills.

    Args:
        registry: SkillRegistry instance with skills already registered.
        max_skills: Maximum number of skills to route to (default: all).
        fallback_signal: Signal to return when no skills are available.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        max_skills: Optional[int] = None,
    ):
        self._registry = registry
        self._max_skills = max_skills

    def route(self, market_data: MarketData) -> List[BaseSkill]:
        """Select the best-fit skills for the given market data.

        Args:
            market_data: Market data with regime information.

        Returns:
            Ordered list of skills, best-fit first.
        """
        regime = market_data.market_regime
        if regime == MarketRegime.UNKNOWN:
            # Infer regime from price data if available
            regime = self._infer_regime(market_data)

        # Get skills that support this regime
        candidates = self._registry.filter_by_regime(regime)
        if not candidates:
            logger.warning(
                "No skills support regime '%s'; falling back to all skills",
                regime.value,
            )
            candidates = self._registry.list_all()

        # Rank by preference order
        preference = REGIME_PREFERENCE.get(regime, REGIME_PREFERENCE[MarketRegime.UNKNOWN])
        ranked = self._rank_skills(candidates, preference)

        # Apply max_skills limit
        if self._max_skills is not None:
            ranked = ranked[: self._max_skills]

        logger.info(
            "Routed to %d skills for %s regime: [%s]",
            len(ranked),
            regime.value,
            ", ".join(s.name for s in ranked),
        )
        return ranked

    def analyze(self, market_data: MarketData) -> List[Signal]:
        """Route to best skills and run analysis on each.

        Args:
            market_data: Market data to analyze.

        Returns:
            List of Signal objects from each selected skill.
        """
        skills = self.route(market_data)
        signals: List[Signal] = []

        for skill in skills:
            try:
                signal = skill.analyze(market_data)
                signals.append(signal)
                logger.debug(
                    "%s → %s (conf=%.2f)",
                    skill.name, signal.direction.value, signal.confidence,
                )
            except Exception as e:
                logger.warning("Skill '%s' analysis failed: %s", skill.name, e)
                # Append a neutral signal so downstream isn't confused
                signals.append(Signal(
                    direction=SignalDirection.HOLD,
                    confidence=0.0,
                    reason=f"Skill '{skill.name}' failed: {e}",
                ))

        return signals

    # ── Regime inference ─────────────────────────────────────────────────────

    @staticmethod
    def _infer_regime(market_data: MarketData) -> MarketRegime:
        """Infer market regime from price data using simple heuristics.

        Uses the 50-period and 200-period SMA crossover (if enough data),
        or a shorter window as fallback.
        """
        prices = market_data.prices
        if len(prices) < 20:
            return MarketRegime.UNKNOWN

        # Use available window for SMA
        window_short = min(50, len(prices))
        window_long = min(200, len(prices))

        sma_short = sum(prices[-window_short:]) / window_short
        sma_long = sum(prices[-window_long:]) / window_long

        # Simple trend detection
        if sma_short > sma_long * 1.02:
            return MarketRegime.BULL
        elif sma_short < sma_long * 0.98:
            return MarketRegime.BEAR
        else:
            return MarketRegime.RANGEBOUND

    # ── Ranking helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _rank_skills(
        skills: List[BaseSkill],
        preference: List[str],
    ) -> List[BaseSkill]:
        """Rank skills by preference order and specialization.

        Skills listed earlier in ``preference`` get higher rank.
        Within the same preference tier, more specialized skills rank higher.
        """
        def sort_key(skill: BaseSkill) -> tuple:
            # Primary: preference order (lower = better)
            try:
                pref_rank = preference.index(skill.name)
            except ValueError:
                pref_rank = len(preference)  # Not in preference list → lowest
            # Secondary: specialization (fewer regimes = more specialized → lower number)
            spec_score = len(skill.supported_regimes)
            return (pref_rank, spec_score)

        return sorted(skills, key=sort_key)

    def to_dict(self) -> Dict:
        """Serialize router config for debugging."""
        return {
            "registry_size": self._registry.count(),
            "max_skills": self._max_skills,
            "registered_skills": self._registry.list_names(),
        }
