"""Simulated Trading Executor — Orchestrator-level signal gating and execution.

Sits between the Agent analysis pipeline and the SimTradingService (Phase 5).
Applies configurable confidence threshold, Kelly-criterion position sizing,
and delegates order placement to the sim trading API.

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
    AccountInfo,
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
    """Orchestrator-level executor that gates and sizes simulated trades.

    Responsibilities:
        1. should_execute(signal) — confidence gating (threshold configurable)
        2. calculate_position_size(signal, account) — Kelly formula or fixed %
        3. execute(signal) — convert to SignalInput and delegate to SimTradingService
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

    # ── Step 7.3: Kelly position sizing ──────────────────────────────────

    def calculate_position_size(
        self,
        signal: TradeSignal,
        account: AccountInfo,
    ) -> float:
        """Calculate position size using Kelly criterion or fixed percentage.

        Kelly formula: f = (p * b - q) / b
            p = win probability (signal.win_rate or signal.confidence)
            b = odds ratio (signal.odds_ratio, default 2.0)
            q = 1 - p

        Uses half-Kelly (f * 0.5) for conservative sizing.

        Args:
            signal: The trade signal with sizing parameters.
            account: Current account info with total_assets.

        Returns:
            Dollar amount to allocate for this trade (0.0 if sizing says skip).
        """
        if account.total_assets <= 0:
            logger.warning("Account total assets is zero — cannot size position")
            return 0.0

        if signal.use_kelly:
            p = signal.win_rate if signal.win_rate is not None else signal.confidence
            b = signal.odds_ratio
            q = 1.0 - p

            # Kelly fraction: f = (p * b - q) / b
            kelly_f = (p * b - q) / b if b > 0 else 0.0

            # Clamp to [0, 1] — negative Kelly means don't bet
            kelly_f = max(0.0, min(1.0, kelly_f))

            # Half-Kelly for safety
            allocation = account.total_assets * kelly_f * 0.5

            logger.info(
                "Kelly sizing for %s: p=%.3f b=%.2f q=%.3f → f=%.4f "
                "half_kelly=%.4f → allocation=%.2f",
                signal.symbol, p, b, q, kelly_f, kelly_f * 0.5, allocation,
            )
        else:
            allocation = account.total_assets * signal.max_position_pct
            logger.info(
                "Fixed sizing for %s: %.1f%% of %.2f → allocation=%.2f",
                signal.symbol,
                signal.max_position_pct * 100,
                account.total_assets,
                allocation,
            )

        return allocation

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
