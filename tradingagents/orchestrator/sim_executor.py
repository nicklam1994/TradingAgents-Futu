"""Simulated Trading Executor — Orchestrator-level signal gating and execution.

Sits between the Agent analysis pipeline and the SimTradingService (Phase 5).
Applies configurable confidence threshold for signal gating, then delegates
order placement (including position sizing) to the sim trading API.

NOTE: Position sizing (Kelly criterion, fixed %) is handled exclusively by
SimTradingService.execute_signal() to avoid double-Kelly bugs.  This module
only gates whether a signal should be forwarded.

Usage:
    executor = SimExecutor(confidence_threshold=0.7)
    if executor.should_execute(signal):
        result = executor.execute(signal)

Dependencies:
    - SimTradingService (api.services.sim_trading_service) for account/positions/orders
    - Signal dataclass for typed input
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from api.services.sim_trading_service import (
    SignalInput,
    SignalResult,
    execute_signal,
    get_account,
)

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class TradeSignal:
    """Typed trade signal from the Agent analysis pipeline.

    Attributes:
        symbol: Stock ticker (e.g., "HK.00700", "US.AAPL")
        signal: Direction — "buy", "sell", or "hold"
        confidence: Model confidence score, 0.0–1.0
        target_price: Optional limit price; executor fetches market price if None
        stop_loss_price: Optional stop-loss trigger price
        max_position_pct: Max position as fraction of total assets (default 0.25)
        use_kelly: Whether to use Kelly criterion for sizing (default True)
        win_rate: Historical win rate for Kelly formula (default from confidence)
        odds_ratio: Payout ratio for Kelly formula (default 2.0 = 2:1)
        metadata: Arbitrary extra fields from the analysis (reports, reasoning, etc.)
    """
    symbol: str
    signal: str
    confidence: float
    target_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    max_position_pct: float = 0.25
    use_kelly: bool = True
    win_rate: Optional[float] = None
    odds_ratio: float = 2.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── SimExecutor ──────────────────────────────────────────────────────────────

class SimExecutor:
    """Orchestrator-level executor that gates and forwards simulated trades.

    Responsibilities:
        1. should_execute(signal) — confidence gating (threshold configurable)
        2. execute(signal) — convert to SignalInput and delegate to SimTradingService

    Position sizing (Kelly, fixed %) is handled by SimTradingService.execute_signal()
    to avoid duplicate half-Kelly application.
    """

    def __init__(self, confidence_threshold: float = 0.7):
        """Initialize the executor.

        Args:
            confidence_threshold: Minimum confidence to execute a trade (default 0.7).
                                  Set lower for more aggressive execution.
        """
        self.confidence_threshold = confidence_threshold

    # ── Step 7.2: Signal gating ──────────────────────────────────────────

    def should_execute(self, signal: TradeSignal) -> bool:
        """Check whether a signal passes the confidence gate.

        A signal is executed only when:
        - signal is not "hold"
        - confidence >= self.confidence_threshold

        Args:
            signal: The trade signal from the Agent pipeline.

        Returns:
            True if the signal should be executed, False otherwise.
        """
        # Hold signals are never executed
        if signal.signal.strip().lower() == "hold":
            logger.info(
                "Signal HOLD for %s — skipping execution", signal.symbol
            )
            return False

        # Confidence gate
        if signal.confidence < self.confidence_threshold:
            logger.info(
                "Signal for %s confidence %.2f < threshold %.2f — skipping",
                signal.symbol,
                signal.confidence,
                self.confidence_threshold,
            )
            return False

        # Must be buy or sell
        if signal.signal.strip().lower() not in ("buy", "sell"):
            logger.warning(
                "Unknown signal '%s' for %s — skipping",
                signal.signal,
                signal.symbol,
            )
            return False

        return True

    # ── Step 7.4: Execute via SimTradingService ──────────────────────────

    def execute(self, signal: TradeSignal) -> SignalResult:
        """Execute a trade signal through the SimTradingService.

        Applies confidence gate, calculates position size, then delegates
        to the service layer for order placement.

        Args:
            signal: The trade signal to execute.

        Returns:
            SignalResult with action_taken, order details, or skip reason.
        """
        # Gate check
        if not self.should_execute(signal):
            return SignalResult(
                action_taken="skipped",
                symbol=signal.symbol,
                reason=(
                    f"Confidence {signal.confidence:.2f} below threshold "
                    f"{self.confidence_threshold:.2f} or signal is HOLD"
                ),
            )

        # Build SignalInput for the service layer
        service_signal = SignalInput(
            symbol=signal.symbol,
            signal=signal.signal,
            confidence=signal.confidence,
            target_price=signal.target_price,
            stop_loss_price=signal.stop_loss_price,
            max_position_pct=signal.max_position_pct,
            use_kelly=signal.use_kelly,
        )

        # Delegate to SimTradingService
        try:
            result = execute_signal(service_signal)
            logger.info(
                "Executed signal for %s: %s — %s",
                signal.symbol,
                result.action_taken,
                result.reason,
            )
            return result
        except Exception as e:
            logger.error(
                "Execution failed for %s: %s", signal.symbol, e, exc_info=True
            )
            return SignalResult(
                action_taken="skipped",
                symbol=signal.symbol,
                reason=f"Execution error: {e}",
            )

    def execute_from_dict(self, signal_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Convenience method: accept a raw dict, return a result dict.

        Useful for API endpoints that receive JSON payloads.

        Args:
            signal_dict: Keys matching TradeSignal fields.

        Returns:
            Dict with action_taken, order_id, symbol, side, quantity, price, reason.
        """
        signal = TradeSignal(
            symbol=signal_dict.get("symbol", ""),
            signal=signal_dict.get("signal", "hold"),
            confidence=float(signal_dict.get("confidence", 0.0)),
            target_price=signal_dict.get("target_price"),
            stop_loss_price=signal_dict.get("stop_loss_price"),
            max_position_pct=float(signal_dict.get("max_position_pct", 0.25)),
            use_kelly=signal_dict.get("use_kelly", True),
            win_rate=signal_dict.get("win_rate"),
            odds_ratio=float(signal_dict.get("odds_ratio", 2.0)),
            metadata=signal_dict.get("metadata", {}),
        )
        result = self.execute(signal)
        return {
            "action_taken": result.action_taken,
            "order_id": result.order_id,
            "symbol": result.symbol,
            "side": result.side,
            "quantity": result.quantity,
            "price": result.price,
            "reason": result.reason,
            "kelly_fraction": result.kelly_fraction,
        }
