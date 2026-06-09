"""Strategy Performance Service — Per-strategy trade performance tracking.

Reads sim_deals records and computes per-strategy metrics (win rate,
Sharpe ratio, max drawdown, total PnL) using FIFO matching of
BUY/SELL pairs.  Provides ranking and auto-selection of the best
performing strategy.

Usage:
    from api.services.strategy_performance_service import StrategyPerformanceService
    svc = StrategyPerformanceService()
    best = svc.get_best_strategy()
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.database import SessionLocal, SimDealDB, engine

logger = logging.getLogger(__name__)

# Minimum completed round-trip trades required before a strategy
# qualifies for ranking / best-strategy selection.
MIN_TRADES_FOR_RANKING = 3


@dataclass
class TradeLot:
    """A single open lot in a FIFO position queue."""
    price: float
    qty: float
    time: str


@dataclass
class ClosedTrade:
    """A completed round-trip trade (BUY → SELL)."""
    symbol: str
    buy_price: float
    sell_price: float
    qty: float
    return_pct: float  # (sell - buy) / buy
    pnl: float          # (sell_price - buy_price) * qty
    buy_time: str
    sell_time: str


@dataclass
class StrategyMetrics:
    """Aggregated performance metrics for a single strategy."""
    strategy_name: str
    total_trades: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    total_pnl: float = 0.0
    composite_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 4),
            "avg_return_pct": round(self.avg_return_pct, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "total_pnl": round(self.total_pnl, 2),
            "composite_score": round(self.composite_score, 4),
        }


class StrategyPerformanceService:
    """Compute and rank per-strategy trading performance.

    Reads from the sim_deals table (which now includes strategy_name)
    and uses FIFO matching to pair BUY and SELL trades into completed
    round-trips, then computes standard metrics.
    """

    def __init__(self, db_factory=None):
        """Initialise the service.

        Args:
            db_factory: Callable returning a SQLAlchemy Session.
                        Defaults to SessionLocal.
        """
        self._db_factory = db_factory or SessionLocal

    # ── Public API ─────────────────────────────────────────────────────

    def get_all_strategies_performance(self) -> List[Dict[str, Any]]:
        """Return all strategies ranked by composite score.

        Returns a list of dicts, each with keys:
            strategy_name, total_trades, win_rate, avg_return_pct,
            sharpe_ratio, max_drawdown, total_pnl, composite_score

        Only strategies with >= MIN_TRADES_FOR_RANKING completed trades
        are included.
        """
        strategies = self._compute_all_strategies()
        # Filter to those with enough trades
        qualified = [
            s for s in strategies if s.total_trades >= MIN_TRADES_FOR_RANKING
        ]
        # Sort by composite score descending
        qualified.sort(key=lambda s: s.composite_score, reverse=True)
        return [s.to_dict() for s in qualified]

    def get_best_strategy(self) -> Optional[str]:
        """Return the strategy_name of the best-performing strategy.

        Returns None if no strategy has enough data to qualify.
        """
        ranked = self.get_all_strategies_performance()
        if ranked:
            return ranked[0]["strategy_name"]
        return None

    def record_trade_result(
        self,
        strategy_name: str,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        pnl: float = 0.0,
        currency: str = "HKD",
    ) -> None:
        """Record a trade result in the sim_deals table.

        Called after each trade execution so the strategy_name is
        associated with the deal record.

        Args:
            strategy_name: YAML strategy identifier
            symbol: Stock code (e.g. "HK.00700")
            side: "BUY" or "SELL"
            price: Execution price
            qty: Quantity traded
            pnl: Realised PnL (for SELL trades)
            currency: Currency code
        """
        db = self._db_factory()
        try:
            deal = SimDealDB(
                id=uuid4().hex,
                order_id=f"PERF-{uuid4().hex[:12]}",
                deal_id=f"PERF-{uuid4().hex[:12]}",
                code=symbol,
                stock_name="",
                trd_side=side.upper(),
                deal_market="HK" if symbol.startswith("HK.") else "US",
                order_type="NORMAL",
                qty=qty,
                price=price,
                create_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                status="FILLED",
                currency=currency,
                strategy_name=strategy_name,
            )
            db.add(deal)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("[strategy-perf] failed to record trade: %s", exc)
        finally:
            db.close()

    # ── Internal Metrics Computation ───────────────────────────────────

    def _compute_all_strategies(self) -> List[StrategyMetrics]:
        """Compute metrics for every strategy found in sim_deals."""
        deals = self._load_deals()
        if not deals:
            return []

        # Group deals by strategy_name
        by_strategy: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for d in deals:
            name = d.get("strategy_name") or "unknown"
            by_strategy[name].append(d)

        results: List[StrategyMetrics] = []
        for strategy_name, strategy_deals in by_strategy.items():
            metrics = self._compute_metrics(strategy_name, strategy_deals)
            results.append(metrics)

        return results

    def _load_deals(self) -> List[Dict[str, Any]]:
        """Load all FILLED sim_deals from the database."""
        db = self._db_factory()
        try:
            rows = (
                db.query(SimDealDB)
                .filter(SimDealDB.status == "FILLED")
                .order_by(SimDealDB.create_time.asc())
                .all()
            )
            deals = []
            for r in rows:
                deals.append({
                    "code": str(r.code),
                    "trd_side": str(r.trd_side),
                    "qty": float(str(r.qty)),
                    "price": float(str(r.price)),
                    "create_time": str(r.create_time or ""),
                    "strategy_name": str(r.strategy_name or "unknown"),
                    "currency": str(getattr(r, "currency", "HKD") or "HKD"),
                })
            return deals
        except Exception as exc:
            logger.error("[strategy-perf] failed to load deals: %s", exc)
            return []
        finally:
            db.close()

    def _compute_metrics(
        self, strategy_name: str, deals: List[Dict[str, Any]]
    ) -> StrategyMetrics:
        """Compute performance metrics for a single strategy.

        Uses FIFO matching: for each SELL, match against the oldest
        unmatched BUY lots for the same symbol.

        Args:
            strategy_name: Strategy identifier
            deals: List of deal dicts (sorted by create_time)

        Returns:
            StrategyMetrics with all fields populated.
        """
        # FIFO position queue: symbol → list of TradeLot
        open_lots: Dict[str, List[TradeLot]] = defaultdict(list)
        closed_trades: List[ClosedTrade] = []

        for deal in deals:
            symbol = deal["code"]
            side = deal["trd_side"].upper()
            price = deal["price"]
            qty = deal["qty"]
            time = deal["create_time"]

            if side == "BUY":
                open_lots[symbol].append(
                    TradeLot(price=price, qty=qty, time=time)
                )
            elif side == "SELL":
                remaining = qty
                while remaining > 0 and open_lots[symbol]:
                    lot = open_lots[symbol][0]
                    match_qty = min(remaining, lot.qty)

                    # Compute return and PnL for this matched pair
                    buy_price = lot.price
                    sell_price = price
                    if buy_price > 0:
                        ret_pct = (sell_price - buy_price) / buy_price
                    else:
                        ret_pct = 0.0
                    pnl = (sell_price - buy_price) * match_qty

                    closed_trades.append(ClosedTrade(
                        symbol=symbol,
                        buy_price=buy_price,
                        sell_price=sell_price,
                        qty=match_qty,
                        return_pct=ret_pct,
                        pnl=pnl,
                        buy_time=lot.time,
                        sell_time=time,
                    ))

                    lot.qty -= match_qty
                    remaining -= match_qty
                    if lot.qty <= 0:
                        open_lots[symbol].pop(0)

        # ── Aggregate metrics ──────────────────────────────────────────
        metrics = StrategyMetrics(strategy_name=strategy_name)
        metrics.total_trades = len(closed_trades)

        if not closed_trades:
            return metrics

        # Win rate
        wins = sum(1 for t in closed_trades if t.pnl > 0)
        metrics.win_rate = wins / len(closed_trades)

        # Average return %
        returns = [t.return_pct for t in closed_trades]
        metrics.avg_return_pct = sum(returns) / len(returns)

        # Total PnL
        metrics.total_pnl = sum(t.pnl for t in closed_trades)

        # Sharpe ratio (on per-trade returns, annualised with √252)
        try:
            if len(returns) >= 2:
                from tradingagents.dataflows.quant_metrics import QuantMetrics
                metrics.sharpe_ratio = QuantMetrics.sharpe_ratio(returns)
            else:
                metrics.sharpe_ratio = 0.0
        except Exception:
            metrics.sharpe_ratio = 0.0

        # Max drawdown (on cumulative PnL equity curve)
        try:
            cum_pnl = 0.0
            equity_curve = [0.0]  # Start at 0
            for t in closed_trades:
                cum_pnl += t.pnl
                equity_curve.append(cum_pnl)
            if len(equity_curve) >= 2:
                from tradingagents.dataflows.quant_metrics import QuantMetrics
                metrics.max_drawdown = QuantMetrics.max_drawdown(equity_curve)
            else:
                metrics.max_drawdown = 0.0
        except Exception:
            metrics.max_drawdown = 0.0

        # Composite score (weighted combination for ranking)
        # Higher win_rate, higher sharpe, higher avg_return, lower drawdown = better
        metrics.composite_score = (
            0.30 * metrics.win_rate
            + 0.25 * min(max(metrics.sharpe_ratio, -2.0), 3.0) / 3.0  # Normalise sharpe to ~[0,1]
            + 0.25 * min(max(metrics.avg_return_pct * 10, -1.0), 1.0)  # Normalise avg return
            + 0.20 * (1.0 - min(metrics.max_drawdown, 1.0))  # Penalise drawdown
        )

        return metrics
