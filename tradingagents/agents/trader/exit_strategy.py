"""Exit Strategy — Stop-loss, take-profit, and trailing-stop logic for positions.

Encapsulates all exit decision logic so the Observer and Trader agents can
delegate exit decisions without duplicating threshold math.

Three built-in strategies:
    1. FixedStopLoss   — exit when loss exceeds a fixed percentage (default -5%)
    2. TrailingStop    — exit when price drops X% from highest observed (default 3%)
    3. TakeProfit       — exit when profit exceeds target (default +15%)

Usage:
    strategy = ExitStrategy()
    decision = strategy.evaluate(entry_price=100, current_price=94, highest_price=108)
    # decision.should_exit == True  (fixed stop-loss at -5%)
    # decision.reason == "stop_loss"

    decision = strategy.evaluate(entry_price=100, current_price=112, highest_price=115)
    # decision.should_exit == True  (take-profit at +15% is not hit here, but trailing stop
    #   checks highest_price 115 → current 112 = 2.6% drop < 3% → no exit)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────────

class ExitReason(str, Enum):
    """Why an exit was triggered."""
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    TAKE_PROFIT = "take_profit"
    NO_EXIT = "no_exit"


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ExitDecision:
    """Result of an exit strategy evaluation.

    Attributes:
        should_exit: Whether the position should be closed.
        reason: Which strategy triggered the exit (or NO_EXIT).
        pnl_pct: Profit/loss percentage at current price.
        threshold: The threshold value that was compared against.
        message: Human-readable explanation of the decision.
        metadata: Extra context (highest_price, drop_from_peak, etc.)
    """
    should_exit: bool
    reason: ExitReason
    pnl_pct: float = 0.0
    threshold: float = 0.0
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for API/logging."""
        return {
            "should_exit": self.should_exit,
            "reason": self.reason.value,
            "pnl_pct": round(self.pnl_pct, 4),
            "threshold": self.threshold,
            "message": self.message,
            **self.metadata,
        }


# ── Exit Strategy ────────────────────────────────────────────────────────────

class ExitStrategy:
    """Composite exit strategy combining fixed stop-loss, trailing stop, and take-profit.

    Each threshold is independently configurable.  All three are evaluated on
    every call to ``evaluate()``; the first one that triggers wins (priority:
    stop-loss > trailing-stop > take-profit).

    Args:
        stop_loss_pct:   Fixed stop-loss as negative fraction (default -0.05 = -5%).
        trailing_pct:    Trailing stop distance from peak (default 0.03 = 3%).
        take_profit_pct: Take-profit target as positive fraction (default 0.15 = +15%).

    Example:
        >>> es = ExitStrategy(stop_loss_pct=-0.05, trailing_pct=0.03, take_profit_pct=0.15)
        >>> d = es.evaluate(entry_price=100, current_price=94)
        >>> d.should_exit, d.reason
        (True, ExitReason.STOP_LOSS)
    """

    def __init__(
        self,
        stop_loss_pct: float = -0.05,
        trailing_pct: float = 0.03,
        take_profit_pct: float = 0.15,
    ):
        # Validate thresholds
        if stop_loss_pct > 0:
            raise ValueError(f"stop_loss_pct must be negative, got {stop_loss_pct}")
        if trailing_pct < 0 or trailing_pct > 1:
            raise ValueError(f"trailing_pct must be 0-1, got {trailing_pct}")
        if take_profit_pct < 0:
            raise ValueError(f"take_profit_pct must be non-negative, got {take_profit_pct}")

        self._stop_loss_pct = stop_loss_pct
        self._trailing_pct = trailing_pct
        self._take_profit_pct = take_profit_pct

    @property
    def stop_loss_pct(self) -> float:
        return self._stop_loss_pct

    @property
    def trailing_pct(self) -> float:
        return self._trailing_pct

    @property
    def take_profit_pct(self) -> float:
        return self._take_profit_pct

    # ── Core evaluation ──────────────────────────────────────────────────────

    def evaluate(
        self,
        entry_price: float,
        current_price: float,
        highest_price: Optional[float] = None,
        side: str = "long",
    ) -> ExitDecision:
        """Evaluate all exit conditions and return the first triggered decision.

        Priority order: stop-loss → trailing stop → take-profit → no exit.

        Args:
            entry_price:   Price at which the position was opened.
            current_price: Latest market price.
            highest_price: Highest price observed since entry (for trailing stop).
                           If None, defaults to max(entry_price, current_price).
            side:          "long" or "short".  Short positions invert the logic.

        Returns:
            ExitDecision with should_exit, reason, and context.
        """
        if entry_price <= 0:
            logger.warning("Invalid entry_price=%s, returning no-exit", entry_price)
            return ExitDecision(
                should_exit=False,
                reason=ExitReason.NO_EXIT,
                message="Invalid entry price",
            )

        # Compute P&L
        pnl_pct = self._calc_pnl(entry_price, current_price, side)

        # Resolve highest_price for trailing stop
        if highest_price is None:
            highest_price = max(entry_price, current_price)

        # 1. Fixed stop-loss (highest priority — protects capital)
        decision = self._check_stop_loss(pnl_pct, side)
        if decision:
            return decision

        # 2. Trailing stop (locks in gains)
        decision = self._check_trailing_stop(current_price, highest_price, side)
        if decision:
            decision.pnl_pct = pnl_pct  # Attach P&L context
            return decision

        # 3. Take-profit (reaches profit target)
        decision = self._check_take_profit(pnl_pct, side)
        if decision:
            return decision

        # No exit
        return ExitDecision(
            should_exit=False,
            reason=ExitReason.NO_EXIT,
            pnl_pct=pnl_pct,
            message=f"No exit: P&L {pnl_pct:+.2%}, all thresholds within range",
        )

    # ── Individual strategy checks ───────────────────────────────────────────

    def _check_stop_loss(self, pnl_pct: float, side: str) -> Optional[ExitDecision]:
        """Check fixed stop-loss condition."""
        triggered = (
            pnl_pct <= self._stop_loss_pct
            if side == "long"
            else pnl_pct >= abs(self._stop_loss_pct)
        )
        if triggered:
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.STOP_LOSS,
                pnl_pct=pnl_pct,
                threshold=self._stop_loss_pct,
                message=(
                    f"STOP-LOSS triggered: P&L {pnl_pct:+.2%} "
                    f"exceeds threshold {self._stop_loss_pct:+.2%}"
                ),
            )
        return None

    def _check_trailing_stop(
        self, current_price: float, highest_price: float, side: str
    ) -> Optional[ExitDecision]:
        """Check trailing stop — price dropped X% from highest observed."""
        if highest_price <= 0:
            return None

        if side == "long":
            drop_from_peak = (highest_price - current_price) / highest_price
        else:
            # For short: price rising from lowest = bad
            drop_from_peak = (current_price - highest_price) / highest_price if highest_price > 0 else 0

        if drop_from_peak >= self._trailing_pct:
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.TRAILING_STOP,
                threshold=self._trailing_pct,
                message=(
                    f"TRAILING STOP triggered: dropped {drop_from_peak:.2%} "
                    f"from peak {highest_price:.2f} (threshold: {self._trailing_pct:.2%})"
                ),
                metadata={
                    "highest_price": highest_price,
                    "current_price": current_price,
                    "drop_from_peak": round(drop_from_peak, 4),
                },
            )
        return None

    def _check_take_profit(self, pnl_pct: float, side: str) -> Optional[ExitDecision]:
        """Check take-profit condition."""
        triggered = (
            pnl_pct >= self._take_profit_pct
            if side == "long"
            else pnl_pct <= -self._take_profit_pct
        )
        if triggered:
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.TAKE_PROFIT,
                pnl_pct=pnl_pct,
                threshold=self._take_profit_pct,
                message=(
                    f"TAKE-PROFIT reached: P&L {pnl_pct:+.2%} "
                    f"exceeds target {self._take_profit_pct:+.2%}"
                ),
            )
        return None

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _calc_pnl(entry_price: float, current_price: float, side: str) -> float:
        """Calculate P&L percentage."""
        if side == "long":
            return (current_price - entry_price) / entry_price
        else:
            return (entry_price - current_price) / entry_price

    def to_config(self) -> Dict[str, Any]:
        """Serialize strategy config for persistence/display."""
        return {
            "stop_loss_pct": self._stop_loss_pct,
            "trailing_pct": self._trailing_pct,
            "take_profit_pct": self._take_profit_pct,
        }

    def __repr__(self) -> str:
        return (
            f"ExitStrategy(stop_loss={self._stop_loss_pct:+.1%}, "
            f"trailing={self._trailing_pct:.1%}, "
            f"take_profit={self._take_profit_pct:+.1%})"
        )
