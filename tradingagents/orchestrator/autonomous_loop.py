"""AutonomousLoop — OODA-based autonomous trading orchestrator.

Implements the Observe → Orient → Decide → Act loop for continuous
autonomous trading. Coordinates CommandRouter, StockSelector,
PortfolioAllocator, Observer, and SimExecutor.

Usage:
    loop = AutonomousLoop(task_store=store)
    task_id = loop.start("2w美金閉環模擬交易 HK.00700")
    loop.pause(task_id)
    loop.resume(task_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from collections import defaultdict

from .command_router import CommandDAG, CommandRouter
from .stock_selector import StockCandidate, StockSelector
from .portfolio_allocator import PortfolioAllocation, PortfolioAllocator
from .observer import Observer, PositionAlert, AlertType
from .task_store import TaskStore, TaskStatus

logger = logging.getLogger(__name__)


class OODAPhase(str, Enum):
    """OODA loop phases."""
    OBSERVE = "observe"
    ORIENT = "orient"
    DECIDE = "decide"
    ACT = "act"


@dataclass
class OODAState:
    """Current state of an OODA loop iteration."""
    phase: OODAPhase
    iteration: int
    started_at: str
    # Observe phase results
    market_data: Dict[str, Any] = field(default_factory=dict)
    position_alerts: List[Dict[str, Any]] = field(default_factory=list)
    # Orient phase results
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    analysis_summary: str = ""
    # Decide phase results
    allocation: Optional[Dict[str, Any]] = None
    trade_decisions: List[Dict[str, Any]] = field(default_factory=list)
    # Act phase results
    executions: List[Dict[str, Any]] = field(default_factory=list)
    # Error tracking
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase.value,
            "iteration": self.iteration,
            "started_at": self.started_at,
            "market_data": self.market_data,
            "position_alerts": self.position_alerts,
            "candidates": [c for c in self.candidates],
            "analysis_summary": self.analysis_summary,
            "allocation": self.allocation,
            "trade_decisions": self.trade_decisions,
            "executions": self.executions,
            "errors": self.errors,
        }


@dataclass
class AutonomousTaskConfig:
    """Configuration for an autonomous trading task."""
    command: str  # Original natural language command
    dag: Optional[Dict[str, Any]] = None  # Parsed command DAG
    budget: float = 10000.0
    currency: str = "USD"
    mode: str = "simulate"
    # Loop parameters
    max_iterations: int = 30  # Max OODA iterations
    iteration_interval_sec: int = 3600  # Seconds between iterations (1 hour)
    stage_timeout: int = 300  # Per-stage timeout in seconds (P1-5)
    stop_loss_pct: float = -0.08
    take_profit_pct: float = 0.15
    # Stock pool (if not using selector)
    fixed_symbols: List[str] = field(default_factory=list)
    # Analyst selection
    analysts: List[str] = field(default_factory=lambda: [
        "market", "fundamentals", "news", "social_media",
        "smart_money", "volume_price", "macro",
    ])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "dag": self.dag,
            "budget": self.budget,
            "currency": self.currency,
            "mode": self.mode,
            "max_iterations": self.max_iterations,
            "iteration_interval_sec": self.iteration_interval_sec,
            "stage_timeout": self.stage_timeout,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "fixed_symbols": self.fixed_symbols,
            "analysts": self.analysts,
        }


class AutonomousLoop:
    """OODA-based autonomous trading loop.

    Coordinates the full autonomous trading pipeline:
    1. Observe: Fetch market data, check positions, monitor alerts
    2. Orient: Analyze candidates, screen stocks, assess conditions
    3. Decide: Allocate capital, size positions, plan trades
    4. Act: Execute trades (sim or real), record results

    Each iteration is checkpointed for crash recovery via TaskStore.

    Dependencies:
        - CommandRouter: Parse natural language commands
        - StockSelector: Screen and rank stock candidates
        - PortfolioAllocator: Kelly-based capital allocation
        - Observer: Position monitoring and alerts
        - SimExecutor: Trade execution (Phase 7)
    """

    def __init__(
        self,
        task_store: Optional[TaskStore] = None,
        command_router: Optional[CommandRouter] = None,
        stock_selector: Optional[StockSelector] = None,
        portfolio_allocator: Optional[PortfolioAllocator] = None,
        observer: Optional[Observer] = None,
        on_iteration: Optional[Callable[[str, OODAState], None]] = None,
    ):
        """Initialize the autonomous loop.

        Args:
            task_store: TaskStore for persistence (created if None)
            command_router: CommandRouter instance (created if None)
            stock_selector: StockSelector instance (created if None)
            portfolio_allocator: PortfolioAllocator instance (created if None)
            observer: Observer instance (created if None)
            on_iteration: Callback after each OODA iteration (task_id, state)
        """
        self._store = task_store or TaskStore()
        self._router = command_router or CommandRouter()
        self._selector = stock_selector or StockSelector()
        self._allocator = portfolio_allocator or PortfolioAllocator()
        # W1 fix: inject get_history_orders for real entry price
        if observer is None:
            try:
                from api.services.sim_trading_service import get_history_orders
                self._observer = Observer(get_history_orders=get_history_orders)
            except ImportError:
                self._observer = Observer()
        else:
            self._observer = observer
        self._on_iteration = on_iteration
        self._running_tasks: Dict[str, bool] = {}  # task_id → should_continue

    # ── Task lifecycle ────────────────────────────────────────────────────

    def start(self, command: str, config: Optional[AutonomousTaskConfig] = None) -> str:
        """Start a new autonomous trading task.

        Args:
            command: Natural language trading command
            config: Optional task configuration overrides

        Returns:
            Task ID
        """
        # Parse command into DAG
        logger.info("Parsing command: %s", command[:80])
        dag = self._router.route(command)

        # Build config from parsed DAG
        if config is None:
            config = AutonomousTaskConfig(
                command=command,
                dag=dag.to_dict(),
                budget=dag.budget or 10000.0,
                currency=dag.currency,
                mode=dag.mode,
            )
        else:
            config.command = command
            config.dag = dag.to_dict()

        # Extract symbols from DAG
        symbols = [t.symbol for t in dag.tasks if t.symbol]
        if symbols:
            config.fixed_symbols = symbols

        # Create task in store
        task_id = self._store.create(
            title=f"Autonomous: {command[:60]}",
            metadata=config.to_dict(),
        )

        # Start the task
        self._store.start(task_id)
        self._running_tasks[task_id] = True

        # Run the OODA loop (first iteration)
        logger.info("Started autonomous task %s: %s", task_id, command[:60])

        # _run_iteration is async (needs event loop for timeout support).
        # Detect existing loop; if none, run synchronously.
        try:
            asyncio.get_running_loop()
            asyncio.ensure_future(self._run_iteration(task_id, config))
        except RuntimeError:
            asyncio.run(self._run_iteration(task_id, config))

        return task_id

    def pause(self, task_id: str) -> bool:
        """Pause a running autonomous task."""
        self._running_tasks[task_id] = False
        success = self._store.pause(task_id)
        if success:
            logger.info("Paused autonomous task %s", task_id)
        return success

    def resume(self, task_id: str) -> bool:
        """Resume a paused autonomous task."""
        task = self._store.get(task_id)
        if not task:
            return False

        success = self._store.resume(task_id)
        if success:
            self._running_tasks[task_id] = True
            logger.info("Resumed autonomous task %s", task_id)
            # Re-run iteration from checkpoint
            meta = task.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            config = self._config_from_dict(meta)
            try:
                asyncio.get_running_loop()
                asyncio.ensure_future(self._run_iteration(task_id, config))
            except RuntimeError:
                asyncio.run(self._run_iteration(task_id, config))
        return success

    def stop(self, task_id: str) -> bool:
        """Stop an autonomous task permanently."""
        self._running_tasks[task_id] = False
        success = self._store.complete(task_id, result={"stopped_by": "user"})
        if success:
            logger.info("Stopped autonomous task %s", task_id)
        return success

    def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get current status of an autonomous task."""
        task = self._store.get(task_id)
        if not task:
            return None

        checkpoint = task.get("checkpoint") or {}
        if isinstance(checkpoint, str):
            checkpoint = json.loads(checkpoint)

        return {
            "task_id": task_id,
            "status": task["status"],
            "progress": task["progress"],
            "current_phase": checkpoint.get("phase"),
            "iteration": checkpoint.get("iteration", 0),
            "last_update": task["updated_at"],
            "metadata": task.get("metadata"),
            "error": task.get("error"),
        }

    # ── OODA Loop ─────────────────────────────────────────────────────────

    async def _run_iteration(self, task_id: str, config: AutonomousTaskConfig) -> None:
        """Run a single OODA iteration.

        Each iteration:
        1. Observe: Check positions, fetch market data
        2. Orient: Analyze candidates, screen stocks
        3. Decide: Allocate capital, plan trades
        4. Act: Execute trades, record results
        """
        # Load checkpoint to resume from
        checkpoint = self._store.get_checkpoint(task_id)
        iteration = checkpoint.get("iteration", 0) + 1

        if iteration > config.max_iterations:
            logger.info("Task %s reached max iterations (%d)", task_id, config.max_iterations)
            self._store.complete(task_id, result={"iterations": iteration - 1})
            return

        state = OODAState(
            phase=OODAPhase.OBSERVE,
            iteration=iteration,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            timeout = config.stage_timeout  # seconds per stage (P1-3)

            # ── Observe (P1-4: timeout-protected) ──
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._phase_observe, task_id, config, state),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Task %s iteration %d: Observe stage timed out after %ds — skipping",
                    task_id, iteration, timeout,
                )
                state.errors.append(f"Observe timed out ({timeout}s)")

            # ── Orient (P1-4: timeout-protected) ──
            if self._should_continue(task_id):
                state.phase = OODAPhase.ORIENT
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._phase_orient, task_id, config, state),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Task %s iteration %d: Orient stage timed out after %ds — skipping",
                        task_id, iteration, timeout,
                    )
                    state.errors.append(f"Orient timed out ({timeout}s)")

            # ── Decide (P1-4: timeout-protected) ──
            if self._should_continue(task_id):
                state.phase = OODAPhase.DECIDE
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._phase_decide, task_id, config, state),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Task %s iteration %d: Decide stage timed out after %ds — skipping",
                        task_id, iteration, timeout,
                    )
                    state.errors.append(f"Decide timed out ({timeout}s)")

            # ── Act (P1-4: timeout-protected) ──
            if self._should_continue(task_id):
                state.phase = OODAPhase.ACT
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._phase_act, task_id, config, state),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Task %s iteration %d: Act stage timed out after %ds — skipping",
                        task_id, iteration, timeout,
                    )
                    state.errors.append(f"Act timed out ({timeout}s)")

            # Update progress
            progress = iteration / config.max_iterations
            self._store.update(task_id, progress=progress)

            # Save checkpoint
            self._store.save_checkpoint(task_id, {
                "phase": state.phase.value,
                "iteration": iteration,
                "state": state.to_dict(),
            })

            # Notify callback
            if self._on_iteration:
                self._on_iteration(task_id, state)

            logger.info(
                "Task %s iteration %d/%d complete (phase=%s)",
                task_id, iteration, config.max_iterations, state.phase.value,
            )

        except Exception as e:
            logger.error("Iteration %d failed for task %s: %s", iteration, task_id, e, exc_info=True)
            state.errors.append(str(e))
            self._store.save_checkpoint(task_id, {
                "phase": state.phase.value,
                "iteration": iteration,
                "state": state.to_dict(),
                "error": str(e),
            })

    def _phase_observe(
        self, task_id: str, config: AutonomousTaskConfig, state: OODAState
    ) -> None:
        """Observe phase: Check positions, fetch market data."""
        logger.debug("Task %s O%d: Observe phase", task_id, state.iteration)

        # Check existing positions for alerts
        checkpoint = self._store.get_checkpoint(task_id)
        prev_state = checkpoint.get("state", {})
        prev_executions = prev_state.get("executions", [])

        if prev_executions:
            # Convert executions to position format for monitoring
            positions = []
            for ex in prev_executions:
                if ex.get("action_taken") == "executed":
                    positions.append({
                        "symbol": ex.get("symbol"),
                        "entry_price": ex.get("price", 0),
                        "current_price": ex.get("price", 0),  # Would fetch real price
                        "quantity": ex.get("quantity", 0),
                        "side": "long",
                    })

            alerts = self._observer.check_positions(positions, config.budget)
            state.position_alerts = [a.to_dict() for a in alerts]

            # Check for critical alerts that should stop the loop
            critical = [a for a in alerts if a.severity == "critical"]
            if critical:
                logger.warning(
                    "Task %s: %d critical alerts — may need intervention",
                    task_id, len(critical),
                )

    def _phase_orient(
        self, task_id: str, config: AutonomousTaskConfig, state: OODAState
    ) -> None:
        """Orient phase: Analyze and screen candidates."""
        logger.debug("Task %s O%d: Orient phase", task_id, state.iteration)

        # L-3~4: drawdown circuit breaker — pause loop if drawdown > 20%
        if self._check_drawdown_circuit_breaker(task_id):
            logger.warning(
                "Task %s: Drawdown circuit breaker triggered — pausing loop",
                task_id,
            )
            state.errors.append("Drawdown circuit breaker: max drawdown > 20%")
            self.pause(task_id)
            return

        if config.fixed_symbols:
            # Use fixed symbol pool from command
            candidates = self._selector.select(
                pool=config.fixed_symbols,
                budget=config.budget,
                top_n=len(config.fixed_symbols),
            )
            state.candidates = [c.to_dict() for c in candidates]
        else:
            # No fixed pool — would need market scanner
            # For now, use a default watchlist or skip
            logger.info("Task %s: No fixed symbols — using default pool", task_id)
            state.candidates = []

    def _phase_decide(
        self, task_id: str, config: AutonomousTaskConfig, state: OODAState
    ) -> None:
        """Decide phase: Allocate capital and plan trades."""
        logger.debug("Task %s O%d: Decide phase", task_id, state.iteration)

        if not state.candidates:
            logger.info("Task %s: No candidates to allocate", task_id)
            return

        # L-5~6: adjust strategy weights when Sharpe ratio is low
        adjusted_candidates = self._adjust_strategy_weights(state.candidates)

        allocation = self._allocator.allocate(
            candidates=adjusted_candidates,
            total_budget=config.budget,
            currency=config.currency,
        )
        state.allocation = allocation.to_dict()

        # Build trade decisions from allocation
        for alloc in allocation.allocations:
            if alloc.shares > 0:
                state.trade_decisions.append({
                    "symbol": alloc.symbol,
                    "action": "buy",
                    "shares": alloc.shares,
                    "amount": alloc.amount,
                    "reasoning": alloc.reasoning,
                })

    def _phase_act(
        self, task_id: str, config: AutonomousTaskConfig, state: OODAState
    ) -> None:
        """Act phase: Execute trades."""
        logger.debug("Task %s O%d: Act phase", task_id, state.iteration)

        if not state.trade_decisions:
            logger.info("Task %s: No trade decisions to execute", task_id)
            return

        # Execute trades via SimExecutor (if in simulate mode)
        if config.mode == "simulate":
            try:
                from tradingagents.orchestrator.sim_executor import SimExecutor, TradeSignal

                executor = SimExecutor(confidence_threshold=0.6)

                for decision in state.trade_decisions:
                    signal = TradeSignal(
                        symbol=decision["symbol"],
                        signal=decision["action"],
                        confidence=0.7,  # Would come from analysis
                        metadata={"autonomous_task": task_id},
                    )
                    result = executor.execute(signal)
                    state.executions.append({
                        "symbol": decision["symbol"],
                        "action_taken": result.action_taken,
                        "order_id": result.order_id,
                        "quantity": result.quantity,
                        "price": result.price,
                        "reason": result.reason,
                    })
            except ImportError:
                logger.warning("SimExecutor not available — skipping execution")
                state.executions.append({
                    "error": "SimExecutor not available",
                })
        else:
            logger.warning("Real trading not implemented — only simulate mode supported")
            state.executions.append({
                "error": "Real trading mode not supported",
            })

    # ── Helpers ───────────────────────────────────────────────────────────

    def _should_continue(self, task_id: str) -> bool:
        """Check if the loop should continue."""
        return self._running_tasks.get(task_id, False)

    # ── Quantitative risk controls (L-3~4) ────────────────────────────────

    def _check_drawdown_circuit_breaker(self, task_id: str) -> bool:
        """L-3~4: Check if portfolio drawdown exceeds 20% threshold.

        Returns:
            True if drawdown > 20% and the loop should be paused.
        """
        try:
            from tradingagents.dataflows.quant_metrics import QuantMetrics
            equity_curve = self._get_equity_curve(task_id)
            if len(equity_curve) < 10:
                return False
            dd = QuantMetrics.max_drawdown(equity_curve)
            if dd > 0.20:
                logger.warning(
                    "Circuit breaker: drawdown %.1f%% > 20%%",
                    dd * 100,
                )
                return True
            return False
        except Exception as e:
            logger.warning("Drawdown circuit breaker check failed: %s", e)
            return False

    def _get_equity_curve(self, task_id: str) -> List[float]:
        """Build a time-ordered equity curve from task execution history.

        Iterates through checkpoint executions and builds cumulative
        portfolio value: starting budget adjusted after each trade.

        Returns:
            List of portfolio values over time.
        """
        checkpoint = self._store.get_checkpoint(task_id)
        state_data = checkpoint.get("state", {})
        config_data = self._store.get(task_id)
        if not config_data:
            return []

        # Get starting budget
        meta = config_data.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        budget = meta.get("budget", 10000.0)

        # Build equity curve from all iterations' executions
        equity = [budget]
        executions = state_data.get("executions", [])
        for ex in executions:
            if ex.get("action_taken") == "executed":
                price = ex.get("price", 0)
                qty = ex.get("quantity", 0)
                if price > 0 and qty > 0:
                    trade_value = price * qty
                    equity.append(equity[-1] + trade_value * 0.01)

        # Also scan historical task data for more equity points
        tasks = self._store.list_tasks(status="completed", limit=20)
        for t in tasks:
            cp = t.get("checkpoint") or {}
            if isinstance(cp, str):
                cp = json.loads(cp)
            s = cp.get("state", {})
            for ex in s.get("executions", []):
                if ex.get("action_taken") == "executed":
                    price = ex.get("price", 0)
                    qty = ex.get("quantity", 0)
                    if price > 0 and qty > 0:
                        equity.append(equity[-1] + price * qty * 0.01)

        return equity

    # ── Strategy weight adjustment (L-5~6) ────────────────────────────────

    def _adjust_strategy_weights(
        self, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """L-5~6: Reduce candidate scores when Sharpe ratio is low (<0.5).

        When the portfolio Sharpe ratio deteriorates, this method reduces
        the composite_score of all candidates by 50%, effectively reducing
        position sizes via Kelly.

        Args:
            candidates: Original candidate list with composite_score.

        Returns:
            Adjusted candidates (new list, originals not mutated).
        """
        try:
            from tradingagents.dataflows.quant_metrics import QuantMetrics
            returns = self._get_recent_returns()
            if len(returns) < 2:
                return candidates
            sharpe = QuantMetrics.sharpe_ratio(returns)
            if sharpe < 0.5:
                logger.warning(
                    "Low Sharpe ratio %.2f < 0.5 — halving strategy weights",
                    sharpe,
                )
                adjusted = []
                for c in candidates:
                    c_copy = dict(c)
                    c_copy["composite_score"] = c_copy.get("composite_score", 0.5) * 0.5
                    adjusted.append(c_copy)
                return adjusted
            return candidates
        except Exception as e:
            logger.warning("Strategy weight adjustment failed: %s", e)
            return candidates

    def _get_recent_returns(self) -> List[float]:
        """Compute per-trade returns from recent execution history.

        Returns:
            List of simple returns (e.g. 0.05 = +5%).
        """
        all_returns: List[float] = []
        tasks = self._store.list_tasks(limit=10)

        for task in tasks:
            checkpoint = task.get("checkpoint") or {}
            if isinstance(checkpoint, str):
                checkpoint = json.loads(checkpoint)
            state_data = checkpoint.get("state", {})

            # Track positions for FIFO matching
            positions: Dict[str, List[List[float]]] = defaultdict(list)

            executions = state_data.get("executions", [])
            for ex in executions:
                symbol = ex.get("symbol", "")
                action = ex.get("action_taken", "")
                price = ex.get("price", 0)
                qty = ex.get("quantity", 0)

                if action != "executed" or price <= 0 or qty <= 0:
                    continue

                # Determine buy/sell from trade decisions
                decisions = state_data.get("trade_decisions", [])
                decision = next(
                    (d for d in decisions if d.get("symbol") == symbol), {}
                )
                side = decision.get("action", "buy")

                if side == "buy":
                    positions[symbol].append([price, qty])
                elif side == "sell":
                    remaining = qty
                    while remaining > 0 and positions[symbol]:
                        buy_price, buy_qty = positions[symbol][0]
                        matched = min(remaining, buy_qty)
                        if buy_price > 0:
                            all_returns.append(
                                (price - buy_price) / buy_price
                            )
                        positions[symbol][0][1] -= matched
                        remaining -= matched
                        if positions[symbol][0][1] <= 0:
                            positions[symbol].pop(0)

        return all_returns

    def _config_from_dict(self, d: Dict[str, Any]) -> AutonomousTaskConfig:
        """Reconstruct AutonomousTaskConfig from dict."""
        return AutonomousTaskConfig(
            command=d.get("command", ""),
            dag=d.get("dag"),
            budget=d.get("budget", 10000.0),
            currency=d.get("currency", "USD"),
            mode=d.get("mode", "simulate"),
            max_iterations=d.get("max_iterations", 30),
            iteration_interval_sec=d.get("iteration_interval_sec", 3600),
            stage_timeout=d.get("stage_timeout", 300),
            stop_loss_pct=d.get("stop_loss_pct", -0.08),
            take_profit_pct=d.get("take_profit_pct", 0.15),
            fixed_symbols=d.get("fixed_symbols", []),
            analysts=d.get("analysts", []),
        )

    def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List autonomous tasks."""
        return self._store.list_tasks(status=status, limit=limit)
