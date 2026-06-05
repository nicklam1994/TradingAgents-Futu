"""Skill Aggregator — Multi-strategy weighted consensus.

Combines signals from multiple strategy skills into a single consensus signal
using configurable weights.  Supports equal weighting, custom per-skill
weights, and confidence-weighted aggregation.

Usage:
    aggregator = SkillAggregator(weights={"momentum": 2.0, "value": 1.5, "mean_reversion": 1.0})
    consensus = aggregator.aggregate(signals)
    # consensus.direction == BUY, consensus.confidence == 0.73
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from tradingagents.skills.base_skill import Signal, SignalDirection

logger = logging.getLogger(__name__)


class SkillAggregator:
    """Aggregates multiple strategy signals into a weighted consensus.

    Aggregation algorithm:
        1. Each signal contributes: direction_score * confidence * weight
        2. Sum all contributions → net score
        3. Net score > 0 → BUY, < 0 → SELL, == 0 → HOLD
        4. Aggregate confidence = |net_score| / total_weight (clamped to 0-1)

    Args:
        weights: Dict mapping skill name → weight multiplier.
                 Skills not in the dict get weight 1.0.
        min_confidence: Minimum aggregate confidence for a non-HOLD signal.
                        If aggregate confidence < min_confidence, force HOLD.
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        min_confidence: float = 0.1,
    ):
        self._weights: Dict[str, float] = weights or {}
        self._min_confidence = min_confidence

    def aggregate(self, signals: List[Signal]) -> Signal:
        """Combine multiple signals into a single consensus.

        Args:
            signals: List of Signal objects from different skills.

        Returns:
            A single Signal representing the weighted consensus.
        """
        if not signals:
            return Signal(
                direction=SignalDirection.HOLD,
                confidence=0.0,
                reason="No signals to aggregate",
            )

        if len(signals) == 1:
            # Single signal — pass through with adjusted reason
            sole = signals[0]
            return Signal(
                direction=sole.direction,
                confidence=sole.confidence,
                reason=f"Single skill consensus: {sole.reason}",
                target_price=sole.target_price,
                stop_loss=sole.stop_loss,
                weight=sole.weight,
                metadata={"source_signals": 1, **sole.metadata},
            )

        # ── Weighted aggregation ─────────────────────────────────────────────
        # Direction encoding: BUY=+1, SELL=-1, HOLD=0
        direction_map = {
            SignalDirection.BUY: 1.0,
            SignalDirection.SELL: -1.0,
            SignalDirection.HOLD: 0.0,
        }

        weighted_sum = 0.0
        total_weight = 0.0
        direction_breakdown: Dict[str, float] = {"buy": 0.0, "sell": 0.0, "hold": 0.0}
        reasons: List[str] = []
        target_prices: List[float] = []
        stop_losses: List[float] = []

        for sig in signals:
            # Resolve weight: use signal's own weight * skill-specific override
            skill_weight = self._get_weight(sig) * sig.weight
            dir_score = direction_map[sig.direction]

            contribution = dir_score * sig.confidence * skill_weight
            weighted_sum += contribution
            total_weight += skill_weight

            direction_breakdown[sig.direction.value] += contribution
            if sig.reason:
                reasons.append(f"[{sig.direction.value}:{sig.confidence:.2f}] {sig.reason}")
            if sig.target_price is not None:
                target_prices.append(sig.target_price)
            if sig.stop_loss is not None:
                stop_losses.append(sig.stop_loss)

        # Compute aggregate confidence
        if total_weight > 0:
            aggregate_confidence = abs(weighted_sum) / total_weight
        else:
            aggregate_confidence = 0.0

        # Clamp to [0, 1]
        aggregate_confidence = max(0.0, min(1.0, aggregate_confidence))

        # Determine consensus direction
        if aggregate_confidence < self._min_confidence:
            consensus_direction = SignalDirection.HOLD
            reason_prefix = "Below min confidence"
        elif weighted_sum > 0:
            consensus_direction = SignalDirection.BUY
            reason_prefix = "Weighted consensus: BUY"
        elif weighted_sum < 0:
            consensus_direction = SignalDirection.SELL
            reason_prefix = "Weighted consensus: SELL"
        else:
            consensus_direction = SignalDirection.HOLD
            reason_prefix = "Equal signals"

        # Average target/stop prices (if available from multiple skills)
        avg_target = sum(target_prices) / len(target_prices) if target_prices else None
        avg_stop = sum(stop_losses) / len(stop_losses) if stop_losses else None

        # Build consensus reason
        consensus_reason = (
            f"{reason_prefix} (score={weighted_sum:+.3f}, "
            f"conf={aggregate_confidence:.2f}, n={len(signals)}).\n"
            + "\n".join(reasons)
        )

        logger.info(
            "Aggregated %d signals → %s (conf=%.2f, score=%+.3f)",
            len(signals),
            consensus_direction.value,
            aggregate_confidence,
            weighted_sum,
        )

        return Signal(
            direction=consensus_direction,
            confidence=aggregate_confidence,
            reason=consensus_reason,
            target_price=avg_target,
            stop_loss=avg_stop,
            weight=total_weight / len(signals),  # average weight
            metadata={
                "source_signals": len(signals),
                "weighted_score": round(weighted_sum, 4),
                "total_weight": round(total_weight, 4),
                "direction_breakdown": {
                    k: round(v, 4) for k, v in direction_breakdown.items()
                },
                "per_signal": [s.to_dict() for s in signals],
            },
        )

    # ── Weight helpers ───────────────────────────────────────────────────────

    def _get_weight(self, signal: Signal) -> float:
        """Resolve weight for a signal based on its metadata skill name.

        Falls back to 1.0 if no skill name found or no weight configured.
        """
        # Signal may carry skill name in metadata
        skill_name = signal.metadata.get("skill_name", "")
        if skill_name and skill_name in self._weights:
            return self._weights[skill_name]
        return 1.0

    def set_weight(self, skill_name: str, weight: float) -> None:
        """Update weight for a specific skill.

        Args:
            skill_name: The skill name to configure.
            weight: New weight multiplier (must be positive).
        """
        if weight <= 0:
            raise ValueError(f"Weight must be positive, got {weight}")
        self._weights[skill_name] = weight

    def get_weights(self) -> Dict[str, float]:
        """Return current weight configuration."""
        return dict(self._weights)

    def to_dict(self) -> Dict:
        """Serialize aggregator config for debugging."""
        return {
            "weights": self._weights,
            "min_confidence": self._min_confidence,
        }
