"""PortfolioAllocator — Kelly criterion-based capital allocation.

Distributes capital across selected stocks using the Kelly criterion
for optimal position sizing, with risk-adjusted constraints.

Usage:
    allocator = PortfolioAllocator()
    allocation = allocator.allocate(
        candidates=selected_stocks,
        total_budget=20000.0,
        currency="USD",
    )
    # allocation = {
    #     "HK.00700": {"amount": 8000.0, "pct": 0.40, "kelly_f": 0.25},
    #     "HK.09988": {"amount": 6000.0, "pct": 0.30, "kelly_f": 0.18},
    #     ...
    # }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from collections import defaultdict

logger = logging.getLogger(__name__)


@dataclass
class AllocationResult:
    """Capital allocation for a single stock."""
    symbol: str
    amount: float           # Dollar amount allocated
    pct: float              # Fraction of total budget (0.0–1.0)
    kelly_f: float          # Raw Kelly fraction
    adjusted_f: float       # After safety adjustments
    shares: int             # Number of shares to buy
    lot_count: int          # Number of lots (for HK/CN: 1 lot = 100 shares)
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "amount": round(self.amount, 2),
            "pct": round(self.pct, 4),
            "kelly_f": round(self.kelly_f, 4),
            "adjusted_f": round(self.adjusted_f, 4),
            "shares": self.shares,
            "lot_count": self.lot_count,
            "reasoning": self.reasoning,
        }


@dataclass
class PortfolioAllocation:
    """Complete portfolio allocation across multiple stocks."""
    total_budget: float
    currency: str
    allocations: List[AllocationResult]
    cash_reserve: float     # Unallocated cash buffer
    total_allocated: float  # Sum of all allocations

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_budget": self.total_budget,
            "currency": self.currency,
            "allocations": [a.to_dict() for a in self.allocations],
            "cash_reserve": round(self.cash_reserve, 2),
            "total_allocated": round(self.total_allocated, 2),
            "utilization_pct": round(
                self.total_allocated / self.total_budget * 100, 1
            ) if self.total_budget > 0 else 0.0,
        }

    def get_by_symbol(self, symbol: str) -> Optional[AllocationResult]:
        """Get allocation for a specific symbol."""
        for a in self.allocations:
            if a.symbol == symbol:
                return a
        return None


class PortfolioAllocator:
    """Kelly criterion-based portfolio allocator.

    Features:
        - Half-Kelly for conservative sizing (configurable fraction)
        - Max position cap (default 25% per stock)
        - Min position threshold (skip tiny allocations)
        - Cash reserve buffer (default 10%)
        - Lot-size rounding for HK/CN markets

    The Kelly formula: f* = (p * b - q) / b
        p = win probability
        b = odds ratio (reward/risk)
        q = 1 - p
        f* = optimal fraction of capital to bet

    We use fractional Kelly (default half) for safety.
    """

    def __init__(
        self,
        kelly_fraction: float = 0.5,
        max_position_pct: float = 0.25,
        min_position_pct: float = 0.05,
        cash_reserve_pct: float = 0.10,
        default_odds_ratio: float = 2.0,
    ):
        """Initialize the allocator.

        Args:
            kelly_fraction: Fraction of Kelly to use (0.5 = half-Kelly, safer)
            max_position_pct: Max allocation per stock (0.25 = 25%)
            min_position_pct: Min allocation to bother with (0.05 = 5%)
            cash_reserve_pct: Cash buffer to keep unallocated (0.10 = 10%)
            default_odds_ratio: Default reward/risk ratio if not specified
        """
        self._kelly_fraction = kelly_fraction
        self._max_position_pct = max_position_pct
        self._min_position_pct = min_position_pct
        self._cash_reserve_pct = cash_reserve_pct
        self._default_odds_ratio = default_odds_ratio

    def allocate(
        self,
        candidates: List[Dict[str, Any]],
        total_budget: float,
        currency: str = "USD",
        custom_odds: Optional[Dict[str, float]] = None,
    ) -> PortfolioAllocation:
        """Allocate capital across candidates using Kelly criterion.

        Args:
            candidates: List of candidate dicts with at least:
                        - symbol: str
                        - composite_score: float (0.0–1.0, used as win probability proxy)
                        - current_price: float (optional, for share calculation)
            total_budget: Total capital to allocate
            currency: Currency code
            custom_odds: Optional per-symbol odds ratios (default: use composite_score)

        Returns:
            PortfolioAllocation with per-stock allocations
        """
        if not candidates:
            return PortfolioAllocation(
                total_budget=total_budget,
                currency=currency,
                allocations=[],
                cash_reserve=total_budget,
                total_allocated=0.0,
            )

        # Step 1: Calculate raw Kelly fractions
        # L-1~2: use historical win_rate for Kelly calibration
        win_rate = self._get_historical_win_rate()
        kelly_fracs = self._calculate_kelly_fractions(
            candidates, custom_odds, historical_win_rate=win_rate,
        )

        # Step 2: Apply caps and normalize
        adjusted_fracs = self._adjust_fractions(kelly_fracs)

        # Step 3: Calculate dollar amounts
        allocatable = total_budget * (1.0 - self._cash_reserve_pct)
        allocations = self._build_allocations(
            candidates, adjusted_fracs, allocatable, currency
        )

        # Step 4: Calculate totals
        total_allocated = sum(a.amount for a in allocations)
        cash_reserve = total_budget - total_allocated

        logger.info(
            "Portfolio allocation: %.0f %s total, %.0f allocated (%.1f%%), %.0f reserve",
            total_budget, currency, total_allocated,
            total_allocated / total_budget * 100 if total_budget > 0 else 0,
            cash_reserve,
        )

        return PortfolioAllocation(
            total_budget=total_budget,
            currency=currency,
            allocations=allocations,
            cash_reserve=cash_reserve,
            total_allocated=total_allocated,
        )

    def _calculate_kelly_fractions(
        self,
        candidates: List[Dict[str, Any]],
        custom_odds: Optional[Dict[str, float]] = None,
        historical_win_rate: Optional[float] = None,
    ) -> Dict[str, float]:
        """Calculate raw Kelly fractions for each candidate.

        Uses historical win_rate when available; falls back to composite_score.
        """
        fractions: Dict[str, float] = {}

        for c in candidates:
            symbol = c["symbol"]
            # L-1: prefer historical win_rate over signal confidence
            p = (
                historical_win_rate
                if historical_win_rate is not None
                else c.get("composite_score", 0.5)
            )
            # Clamp to reasonable range
            p = max(0.1, min(0.9, p))

            # Odds ratio: reward/risk
            b = (
                (custom_odds or {}).get(symbol)
                or c.get("odds_ratio")
                or self._default_odds_ratio
            )
            q = 1.0 - p

            # Kelly formula: f* = (p*b - q) / b
            kelly_f = (p * b - q) / b if b > 0 else 0.0
            # Clamp to [0, 1] — negative means don't bet
            kelly_f = max(0.0, min(1.0, kelly_f))

            fractions[symbol] = kelly_f
            logger.debug(
                "Kelly for %s: p=%.3f b=%.2f q=%.3f → f*=%.4f",
                symbol, p, b, q, kelly_f,
            )

        return fractions

    def _adjust_fractions(
        self, kelly_fracs: Dict[str, float]
    ) -> Dict[str, float]:
        """Apply fractional Kelly and position caps.

        Steps:
            1. Multiply by kelly_fraction (half-Kelly by default)
            2. Cap each position at max_position_pct
            3. Remove positions below min_position_pct
            4. Normalize so total doesn't exceed 1.0
        """
        adjusted: Dict[str, float] = {}

        for symbol, f in kelly_fracs.items():
            # Apply fractional Kelly
            adj_f = f * self._kelly_fraction

            # Cap at max position
            adj_f = min(adj_f, self._max_position_pct)

            # Skip tiny positions
            if adj_f < self._min_position_pct:
                logger.debug(
                    "Skipping %s: adjusted fraction %.4f < min %.4f",
                    symbol, adj_f, self._min_position_pct,
                )
                continue

            adjusted[symbol] = adj_f

        # Normalize if total exceeds allocatable portion
        total_f = sum(adjusted.values())
        if total_f > 1.0 - self._cash_reserve_pct:
            scale = (1.0 - self._cash_reserve_pct) / total_f
            adjusted = {s: f * scale for s, f in adjusted.items()}
            logger.info("Normalized allocations by %.3f to fit budget", scale)

        return adjusted

    def _build_allocations(
        self,
        candidates: List[Dict[str, Any]],
        fracs: Dict[str, float],
        allocatable: float,
        currency: str,
    ) -> List[AllocationResult]:
        """Build AllocationResult objects with share calculations."""
        # Build lookup for candidate data
        candidate_map = {c["symbol"]: c for c in candidates}

        allocations = []
        for symbol, frac in sorted(fracs.items(), key=lambda x: -x[1]):
            c = candidate_map.get(symbol, {})
            amount = allocatable * frac
            price = c.get("current_price", 0)

            # Lot size: 100 for HK/CN, 1 for US
            lot_size = 100 if symbol.startswith(("HK.", "SH.", "SZ.")) else 1

            # Calculate shares, rounded to lot size
            if price and price > 0:
                raw_shares = amount / price
                lot_count = int(raw_shares / lot_size)
                shares = lot_count * lot_size
                # Adjust amount to match actual share count
                actual_amount = shares * price
            else:
                shares = 0
                lot_count = 0
                actual_amount = amount

            allocations.append(AllocationResult(
                symbol=symbol,
                amount=actual_amount,
                pct=frac,
                kelly_f=frac / self._kelly_fraction if self._kelly_fraction > 0 else frac,
                adjusted_f=frac,
                shares=shares,
                lot_count=lot_count,
                reasoning=(
                    f"Kelly f*={frac / self._kelly_fraction:.3f}, "
                    f"half-Kelly={frac:.3f}, "
                    f"{shares} shares @ {price:.2f}"
                ),
            ))

        return allocations

    def _get_historical_win_rate(self) -> float:
        """L-2: Compute win rate from actual simulated trade history.

        Fetches deals from sim_trading_service, FIFO-matches buy/sell pairs,
        and calculates the fraction of profitable trades.

        Returns:
            Win rate in [0, 1]. Defaults to 0.5 when sample size < 5.
        """
        try:
            from api.services.sim_trading_service import get_deals
            deals = get_deals()
            returns = self._calculate_trade_returns(deals)
            if len(returns) < 5:
                logger.info(
                    "Historical win rate: insufficient samples (%d < 5), using default 0.5",
                    len(returns),
                )
                return 0.5
            from tradingagents.dataflows.quant_metrics import QuantMetrics
            wr = QuantMetrics.win_rate(returns)
            logger.info("Historical win rate: %.1f%% (%d trades)", wr * 100, len(returns))
            return wr
        except Exception as e:
            logger.warning("Failed to compute historical win rate: %s — using default 0.5", e)
            return 0.5

    def _calculate_trade_returns(self, deals: list) -> List[float]:
        """FIFO-match buy/sell deals into round-trip trades and compute returns.

        Args:
            deals: List of DealInfo objects with .code, .side, .price, .qty fields.

        Returns:
            List of per-trade simple returns (e.g. 0.05 = +5%).
        """
        # Group by symbol
        buy_queues: Dict[str, list] = defaultdict(list)
        returns: List[float] = []

        for deal in deals:
            symbol = deal.code
            side = (deal.side or "").upper()
            price = deal.price
            qty = deal.qty

            if side == "BUY":
                buy_queues[symbol].append([price, qty])
            elif side == "SELL":
                remaining = qty
                while remaining > 0 and buy_queues[symbol]:
                    buy_price, buy_qty = buy_queues[symbol][0]
                    matched = min(remaining, buy_qty)
                    # Per-share return for this matched pair
                    if buy_price > 0:
                        returns.append((price - buy_price) / buy_price)
                    buy_queues[symbol][0][1] -= matched
                    remaining -= matched
                    if buy_queues[symbol][0][1] <= 0:
                        buy_queues[symbol].pop(0)

        return returns
