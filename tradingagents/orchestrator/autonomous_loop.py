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
    ANALYZE = "analyze"
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
    # Analyze phase results (Disconnection #4: TradingGraph multi-agent analysis)
    analysis_reports: List[Dict[str, Any]] = field(default_factory=list)
    # Orient phase BM25 lessons (Disconnection #7: reflection → future decisions)
    bm25_lessons: List[Dict[str, Any]] = field(default_factory=list)
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
            "analysis_reports": self.analysis_reports,
            "bm25_lessons": self.bm25_lessons,
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
    # Strategy
    strategy_name: Optional[str] = None  # YAML strategy name (e.g. "bull_trend")
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
            "strategy_name": self.strategy_name,
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
    3. Analyze: TradingGraph multi-agent deep analysis (7 analysts → debate → risk)
    4. Decide: Allocate capital, size positions, plan trades
    5. Act: Execute trades (sim or real), record results

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
        llm_api_key: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        strategy_params: Optional[Dict[str, Any]] = None,
    ):
        """Initialize the autonomous loop.

        Args:
            task_store: TaskStore for persistence (created if None)
            command_router: CommandRouter instance (created if None)
            stock_selector: StockSelector instance (created if None)
            portfolio_allocator: PortfolioAllocator instance (created if None)
            observer: Observer instance (created if None)
            on_iteration: Callback after each OODA iteration (task_id, state)
            llm_api_key: LLM API key for CommandRouter/StockSelector
            llm_base_url: LLM base URL
            llm_provider: LLM provider name
            llm_model: LLM model name
            strategy_params: YAML strategy params from get_strategy_params()
        """
        self._store = task_store or TaskStore()
        self._strategy_params = strategy_params

        # ── L3→L5: Inject strategy instructions into CommandRouter prompt ──
        strategy_instructions = ""
        if strategy_params:
            strategy_instructions = strategy_params.get("instructions", "")

        if command_router:
            self._router = command_router
        else:
            self._router = CommandRouter(
                llm_provider=llm_provider,
                llm_model=llm_model,
                api_key=llm_api_key,
                base_url=llm_base_url,
                strategy_instructions=strategy_instructions,
            )

        # ── L3→L4a: Inject YAML dimension_weights into StockSelector ──
        weights = None
        if strategy_params:
            weights = strategy_params.get("dimension_weights")

        self._selector = stock_selector or StockSelector(
            weights=weights,
            llm_provider=llm_provider,
            llm_model=llm_model,
            api_key=llm_api_key,
            base_url=llm_base_url,
        )

        # ── L3→L4b: Inject YAML position_sizing into PortfolioAllocator ──
        if strategy_params and strategy_params.get("position_sizing"):
            ps = strategy_params["position_sizing"]
            self._allocator = portfolio_allocator or PortfolioAllocator(
                kelly_fraction=ps.get("kelly_fraction", 0.5),
                max_position_pct=ps.get("max_position_pct", 20.0) / 100.0,
            )
        else:
            self._allocator = portfolio_allocator or PortfolioAllocator()

        # ── L3→L4c: Inject YAML exit_rules into Observer ──
        if observer is None:
            exit_rules = (strategy_params or {}).get("exit_rules", {})
            try:
                from api.services.sim_trading_service import get_history_orders
                self._observer = Observer(
                    stop_loss_pct=exit_rules.get("stop_loss_pct", -8.0) / 100.0,
                    take_profit_pct=exit_rules.get("take_profit_pct", 15.0) / 100.0,
                    trailing_stop_pct=exit_rules.get("trailing_stop_pct", 5.0) / 100.0,
                    get_history_orders=get_history_orders,
                )
            except ImportError:
                self._observer = Observer(
                    stop_loss_pct=exit_rules.get("stop_loss_pct", -8.0) / 100.0,
                    take_profit_pct=exit_rules.get("take_profit_pct", 15.0) / 100.0,
                    trailing_stop_pct=exit_rules.get("trailing_stop_pct", 5.0) / 100.0,
                )
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
            # Let LLM-parsed values fill in when frontend didn't provide them
            if config.budget == 10000.0 and dag.budget:
                config.budget = dag.budget
                logger.info("LLM parsed budget: %.0f", dag.budget)
            if config.currency == "USD" and dag.currency != "USD":
                config.currency = dag.currency
                logger.info("LLM parsed currency: %s", dag.currency)

        # Extract symbols from DAG (filter out placeholders like FROM_t1, SELECTED_SYMBOL)
        _PLACEHOLDER_PREFIXES = ("FROM_", "SELECTED_", "PLACEHOLDER", "TBD")
        symbols = [
            t.symbol for t in dag.tasks
            if t.symbol and not t.symbol.upper().startswith(_PLACEHOLDER_PREFIXES)
        ]
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
        3. Analyze: TradingGraph multi-agent deep analysis
        4. Decide: Allocate capital, plan trades
        5. Act: Execute trades, record results
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

            # ── Analyze (Disconnection #4: TradingGraph multi-agent) ──
            if self._should_continue(task_id):
                state.phase = OODAPhase.ANALYZE
                try:
                    # propagate_async is truly async — call directly with await
                    analyze_timeout = max(timeout * 3, 600)
                    await asyncio.wait_for(
                        self._phase_analyze(task_id, config, state),
                        timeout=analyze_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Task %s iteration %d: Analyze stage timed out after %ds — skipping",
                        task_id, iteration, analyze_timeout,
                    )
                    state.errors.append(f"Analyze timed out ({analyze_timeout}s)")

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

        # L-7~8: trigger reflection when win_rate drops below 40%
        self._trigger_reflection_if_needed(task_id, config, state)

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
        # W3-2: skip circuit breaker check for one iteration after circuit
        # breaker resume (pause_reason == "circuit_breaker") to avoid
        # immediately re-pausing on the same drawdown data.
        checkpoint = self._store.get_checkpoint(task_id)
        pause_reason = checkpoint.get("pause_reason")
        if pause_reason == "circuit_breaker":
            logger.info(
                "Task %s: Resumed from circuit breaker — skipping one drawdown check",
                task_id,
            )
            # Clear pause_reason so next iteration checks normally
            checkpoint["pause_reason"] = None
            self._store.save_checkpoint(task_id, checkpoint)
        elif self._check_drawdown_circuit_breaker(task_id):
            logger.warning(
                "Task %s: Drawdown circuit breaker triggered — pausing loop",
                task_id,
            )
            state.errors.append("Drawdown circuit breaker: max drawdown > 20%")
            # W3-1: Record pause_reason to distinguish circuit breaker from user pause
            cp = self._store.get_checkpoint(task_id)
            cp["pause_reason"] = "circuit_breaker"
            self._store.save_checkpoint(task_id, cp)
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
            # No fixed pool — use DAG select task to screen stocks via Futu API
            pool = self._screen_stocks_from_dag(config)
            if pool:
                top_n = self._get_select_count_from_dag(config)
                candidates = self._selector.select(
                    pool=pool,
                    budget=config.budget,
                    top_n=top_n,
                )
                state.candidates = [c.to_dict() for c in candidates]
            else:
                logger.info("Task %s: No stocks found from screening", task_id)
                state.candidates = []

        # ── Disconnection #7: Query BM25 lessons from past reflections ──
        self._apply_bm25_lessons(task_id, config, state)

    def _apply_bm25_lessons(self, task_id: str, config: AutonomousTaskConfig, state: OODAState) -> None:
        """Retrieve BM25 lessons from past trade reflections and apply to candidates.

        Builds a situation string from the current task context (market, category,
        horizon, candidate symbols), queries SimTradeReflector.get_relevant_lessons(),
        then adjusts composite_score for matching candidates:
          - Positive lesson (win/positive sentiment) → score +5%
          - Negative lesson (loss/negative sentiment) → score -10%
          - Neutral → no adjustment

        Lessons are stored in state.bm25_lessons for Decide phase reference.
        """
        reflector = self._get_reflector()
        if not reflector:
            return

        if not state.candidates:
            return

        try:
            # Build situation from task context + candidate symbols
            market = "HK" if getattr(config, "currency", "USD") == "HKD" else "US"
            category = ""
            if config.dag:
                for task in config.dag.get("tasks", []):
                    if task.get("action") == "select":
                        params = task.get("params", {})
                        category = params.get("category", "") or params.get("universe", "") or params.get("sector", "")
                        cat_lower = category.lower()
                        if "hk" in cat_lower or "港股" in cat_lower:
                            market = "HK"
                        elif "us" in cat_lower or "美股" in cat_lower:
                            market = "US"
                        break

            symbols = [c.get("symbol", "") for c in state.candidates[:5]]
            situation = (
                f"Trading {market} stocks in {category or 'general market'} sector. "
                f"Candidates: {', '.join(symbols)}. "
                f"Budget: {config.budget} USD. "
                f"Iteration {state.iteration}."
            )

            # Query BM25 for relevant lessons (top 5)
            lessons = reflector.get_relevant_lessons(situation, n_matches=5)
            if not lessons:
                logger.debug("Task %s: No BM25 lessons found", task_id)
                return

            state.bm25_lessons = lessons
            logger.info(
                "Task %s: Found %d BM25 lessons — %s",
                task_id, len(lessons),
                "; ".join(f"[{l.get('similarity_score', 0):.2f}] {l.get('matched_situation', '')[:60]}" for l in lessons[:3]),
            )

            # Apply lessons to candidate scores
            for candidate in state.candidates:
                sym = candidate.get("symbol", "")
                if not sym:
                    continue

                for lesson in lessons:
                    matched = lesson.get("matched_situation", "").upper()
                    recommendation = lesson.get("recommendation", "").lower()
                    similarity = lesson.get("similarity_score", 0)

                    # Only apply high-similarity lessons that mention this symbol
                    if similarity < 0.3:
                        continue
                    if sym.upper() not in matched and sym.split(".")[0].upper() not in matched:
                        continue

                    old_score = candidate.get("composite_score", 0)

                    # Detect sentiment of the lesson
                    negative_keywords = ["loss", "lost", "decline", "drop", "mistake", "wrong", "avoid", "sell", "bearish", "overvalued", "風險", "虧損", "下跌"]
                    positive_keywords = ["profit", "gain", "win", "correct", "good", "strong", "bullish", "undervalued", "盈利", "上漲", "看多"]

                    neg_count = sum(1 for kw in negative_keywords if kw in recommendation)
                    pos_count = sum(1 for kw in positive_keywords if kw in recommendation)

                    if neg_count > pos_count:
                        # Negative lesson → reduce score by 10%
                        adjustment = -0.10
                    elif pos_count > neg_count:
                        # Positive lesson → boost score by 5%
                        adjustment = 0.05
                    else:
                        continue

                    new_score = max(0, old_score * (1 + adjustment))
                    candidate["composite_score"] = round(new_score, 4)
                    logger.info(
                        "Task %s: BM25 adjustment for %s: %.4f → %.4f (%+.0f%%) — %s",
                        task_id, sym, old_score, new_score, adjustment * 100,
                        lesson.get("matched_situation", "")[:80],
                    )
                    break  # Apply only the highest-similarity matching lesson

        except Exception as e:
            logger.debug("BM25 lesson retrieval failed (non-critical): %s", e)

    def _get_select_count_from_dag(self, config: AutonomousTaskConfig) -> int:
        """Extract top_n count from DAG select task params."""
        if not config.dag:
            return 5
        for task in config.dag.get("tasks", []):
            if task.get("action") == "select":
                params = task.get("params", {})
                return params.get("count", params.get("top_n", 5))
        return 5

    def _screen_stocks_from_dag(self, config: AutonomousTaskConfig) -> List[str]:
        """Screen stocks using Futu API based on DAG select task params.

        Two modes:
        1. Structured filter_params (preferred): Build SimpleFilter list
           directly from DAG params and call get_stock_filter.
        2. Fallback (legacy): plate-based keyword matching → get_plate_stock.
        """
        import os

        # Extract select task params from DAG
        market = "HK" if getattr(config, "currency", "USD") == "HKD" else "US"
        category = ""
        horizon = "short"
        top_n = 5
        filter_params = None
        if config.dag:
            for task in config.dag.get("tasks", []):
                if task.get("action") == "select":
                    params = task.get("params", {})
                    # Support both old (category) and new (universe/sector/criteria) param names
                    category = params.get("category", "") or params.get("universe", "") or params.get("sector", "") or params.get("criteria", "")
                    horizon = params.get("horizon", "short")
                    top_n = params.get("count", params.get("top_n", 5))
                    filter_params = params.get("filter_params")
                    # Infer market from category
                    cat_lower = category.lower()
                    if "hk" in cat_lower or "港股" in cat_lower:
                        market = "HK"
                    elif "us" in cat_lower or "美股" in cat_lower:
                        market = "US"
                    break

        logger.info(
            "Task: Screening stocks — market=%s, category='%s', horizon=%s, has_filter_params=%s",
            market, category, horizon, bool(filter_params),
        )

        # ── Path A: Structured filter_params → get_stock_filter ────────
        if filter_params and isinstance(filter_params, dict) and filter_params.get("filters"):
            try:
                return self._screen_with_filter_params(
                    market, category, top_n, filter_params,
                )
            except Exception as e:
                logger.warning(
                    "Structured filter_params screening failed (%s), "
                    "falling back to plate-based approach.", e,
                )
                # Continue to Path B below

        # ── Path B: Legacy plate-based approach ────────────────────────
        try:
            from futu import OpenQuoteContext, Plate, RET_OK

            host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
            port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
            ctx = OpenQuoteContext(host=host, port=port)

            try:
                # Step 1: Get industry plate list
                mkt = {"HK": "HK", "US": "US"}.get(market, "US")
                from futu import Market
                market_enum = Market.HK if mkt == "HK" else Market.US

                ret, plates = ctx.get_plate_list(
                    market=market_enum, plate_class=Plate.INDUSTRY
                )
                if ret != RET_OK:
                    logger.warning("Failed to get plate list: %s", plates)
                    return []

                # Step 2: Find matching sector plates by keyword
                # Map common category keywords to sector names
                keyword_map = {
                    "tech": ["半导体", "互联网", "软件", "计算机", "电子", "Technology", "Semiconductor", "Software"],
                    "科技": ["半导体", "互联网", "软件", "计算机", "电子"],
                    "technology": ["半导体", "互联网", "软件", "计算机", "电子", "Technology", "Semiconductor", "Software"],
                    "semiconductor": ["半导体"],
                    "半导体": ["半导体"],
                    "ai": ["半导体", "互联网", "软件", "人工智能"],
                    "finance": ["银行", "保险", "金融"],
                    "金融": ["银行", "保险", "金融"],
                    "health": ["医疗", "医药", "生物"],
                    "医疗": ["医疗", "医药", "生物"],
                    "consumer": ["消费", "零售", "食品"],
                    "消费": ["消费", "零售", "食品"],
                    "energy": ["能源", "石油", "新能源"],
                    "能源": ["能源", "石油", "新能源"],
                }

                cat_lower = category.lower()
                matched_keywords = []
                for key, keywords in keyword_map.items():
                    if key in cat_lower:
                        matched_keywords.extend(keywords)
                if not matched_keywords:
                    # Default to tech sector
                    matched_keywords = ["半导体", "互联网", "软件", "电子"]

                # Find matching plates
                matched_plates = []
                for _, row in plates.iterrows():
                    plate_name = row.get("plate_name", "")
                    if any(kw in plate_name for kw in matched_keywords):
                        matched_plates.append(row["code"])
                        logger.info("Matched plate: %s (%s)", plate_name, row["code"])

                if not matched_plates:
                    # Fallback: use top plates by name similarity
                    logger.info("No keyword match, using first 3 industry plates")
                    matched_plates = plates["code"].tolist()[:3]

                # Step 3: Get stocks from matched plates
                all_symbols = set()
                for plate_code in matched_plates[:5]:  # Max 5 plates
                    ret, stocks = ctx.get_plate_stock(plate_code)
                    if ret == RET_OK and stocks is not None:
                        # Futu returns 'code' column (not 'stock_code')
                        code_col = "code" if "code" in stocks.columns else "stock_code"
                        for _, row in stocks.iterrows():
                            code = row.get(code_col, "")
                            if code:
                                # Convert Futu format to canonical
                                if code.startswith("HK."):
                                    all_symbols.add(f"{code[3:]}.HK")
                                elif code.startswith("US."):
                                    all_symbols.add(code[3:])

                logger.info(
                    "Screened %d stocks from %d plates",
                    len(all_symbols), len(matched_plates[:5]),
                )

                # Step 4: Filter by market cap (top stocks only)
                if len(all_symbols) > 30:
                    # Use screen_stocks to narrow down
                    from tradingagents.agents.utils.game_theory_tools import screen_stocks
                    screened = screen_stocks.invoke({
                        "market": mkt,
                        "metric": "market_cap",
                        "min_val": 1e9,  # > 1B
                        "limit": 50,
                    })
                    # Parse screened results
                    screened_codes = set()
                    for line in screened.split("\n"):
                        if "|" in line and not line.startswith("|"):
                            parts = [p.strip() for p in line.split("|") if p.strip()]
                            if len(parts) >= 2:
                                code = parts[0]
                                if code.endswith(".HK"):
                                    screened_codes.add(code)
                                else:
                                    screened_codes.add(code)
                    # Intersect with plate stocks
                    filtered = all_symbols & screened_codes
                    if filtered:
                        all_symbols = filtered

                result = list(all_symbols)[:30]  # Cap at 30
                logger.info("Final candidate pool: %d stocks", len(result))
                return result

            finally:
                ctx.close()

        except Exception as e:
            logger.error("Stock screening failed: %s", e, exc_info=True)
            return []

    # ── Structured filter_params screening helper ──────────────────────

    @staticmethod
    def _canonicalize_futu_code(code: str) -> str:
        """Convert Futu code (HK.00700) to canonical form (00700.HK / AAPL)."""
        if code.startswith("HK."):
            return f"{code[3:]}.HK"
        elif code.startswith("US."):
            return code[3:]  # AAPL
        return code

    def _screen_with_filter_params(
        self,
        market: str,
        category: str,
        top_n: int,
        filter_params: dict,
    ) -> List[str]:
        """Use Futu get_stock_filter with structured filter_params.

        Args:
            market: "HK" or "US"
            category: Category string (used to find plate_code)
            top_n: Number of results to return
            filter_params: Dict with keys:
                - filters: list of {field, min, max}
                - sort_field: optional string
                - sort_dir: optional "ASC" or "DESC"
        """
        import os
        from futu import (
            OpenQuoteContext, SimpleFilter, StockField, Market, Plate, RET_OK,
            SortDir,
        )

        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        market_enum = Market.HK if market == "HK" else Market.US

        # Build filter list from structured params
        filter_list = []
        for f_entry in filter_params.get("filters", []):
            field_name = f_entry.get("field", "")
            try:
                stock_field = getattr(StockField, field_name)
            except AttributeError:
                logger.warning("Unknown StockField '%s', skipping filter entry", field_name)
                continue
            sf = SimpleFilter()
            sf.stock_field = stock_field
            sf.filter_min = f_entry.get("min", -1e18) if f_entry.get("min") is not None else -1e18
            sf.filter_max = f_entry.get("max", 1e18) if f_entry.get("max") is not None else 1e18
            sf.is_no_filter = False
            filter_list.append(sf)

        if not filter_list:
            logger.warning("No valid filters built from filter_params, falling back")
            raise ValueError("No valid filters in filter_params")

        # Resolve sort_field → StockField, sort_dir → SortDir
        sort_field_enum = None
        sort_dir_enum = SortDir.ASCEND
        sf_name = filter_params.get("sort_field")
        if sf_name:
            try:
                sort_field_enum = getattr(StockField, sf_name)
            except AttributeError:
                logger.warning("Unknown sort_field '%s', ignoring sort", sf_name)

        sd_name = (filter_params.get("sort_dir") or "DESC").upper()
        if sd_name == "DESC":
            sort_dir_enum = SortDir.DESCEND
        else:
            sort_dir_enum = SortDir.ASCEND

        # Try to narrow by plate_code if category matches a sector
        plate_code = self._resolve_plate_code(market_enum, category)

        ctx = OpenQuoteContext(host=host, port=port)
        try:
            request_kwargs = dict(
                market=market_enum,
                filter_list=filter_list,
                begin=0,
                num=min(top_n * 3, 200),  # request extra for scoring headroom
            )
            if plate_code:
                request_kwargs["plate_code"] = plate_code
                logger.info("Scoping stock filter to plate_code=%s", plate_code)

            ret, result = ctx.get_stock_filter(**request_kwargs)
            if ret != RET_OK:
                logger.warning("get_stock_filter returned error: %s", result)
                raise RuntimeError(f"get_stock_filter error: {result}")

            has_more, total, items = result
            logger.info(
                "get_stock_filter: %d items (total=%d, has_more=%s, plate=%s)",
                len(items), total, has_more, plate_code or "global",
            )

            # Manual sort (get_stock_filter doesn't support sort params)
            if sort_field_enum is not None and items:
                attr_name = sf_name.lower() if sf_name else ""
                reverse = (sort_dir_enum == SortDir.DESCEND)
                try:
                    items = sorted(items, key=lambda x: getattr(x, attr_name, 0) or 0, reverse=reverse)
                except Exception:
                    pass

            # Canonicalize codes
            codes = [self._canonicalize_futu_code(item.stock_code) for item in items]
            return codes[:top_n * 3]  # Return extra pool for downstream scoring

        finally:
            ctx.close()

    def _resolve_plate_code(self, market_enum, category: str):
        """Return the first matching industry plate_code for *category*, or None."""
        if not category:
            return None
        try:
            from futu import OpenQuoteContext, Plate, RET_OK
            import os

            host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
            port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
            ctx = OpenQuoteContext(host=host, port=port)
            try:
                ret, plates = ctx.get_plate_list(
                    market=market_enum, plate_class=Plate.INDUSTRY
                )
                if ret != RET_OK:
                    return None

                keyword_map = {
                    "tech": ["半导体", "互联网", "软件", "计算机", "电子", "Technology", "Semiconductor", "Software"],
                    "科技": ["半导体", "互联网", "软件", "计算机", "电子"],
                    "technology": ["半导体", "互联网", "软件", "计算机", "电子", "Technology", "Semiconductor", "Software"],
                    "semiconductor": ["半导体"],
                    "半导体": ["半导体"],
                    "ai": ["半导体", "互联网", "软件", "人工智能"],
                    "finance": ["银行", "保险", "金融"],
                    "金融": ["银行", "保险", "金融"],
                    "health": ["医疗", "医药", "生物"],
                    "医疗": ["医疗", "医药", "生物"],
                    "consumer": ["消费", "零售", "食品"],
                    "消费": ["消费", "零售", "食品"],
                    "energy": ["能源", "石油", "新能源"],
                    "能源": ["能源", "石油", "新能源"],
                }

                cat_lower = category.lower()
                matched_keywords = []
                for key, keywords in keyword_map.items():
                    if key in cat_lower:
                        matched_keywords.extend(keywords)
                if not matched_keywords:
                    return None

                for _, row in plates.iterrows():
                    plate_name = row.get("plate_name", "")
                    if any(kw in plate_name for kw in matched_keywords):
                        return row["code"]

                return None
            finally:
                ctx.close()
        except Exception as e:
            logger.debug("Plate resolution failed: %s", e)
            return None

    async def _phase_analyze(
        self, task_id: str, config: AutonomousTaskConfig, state: OODAState
    ) -> None:
        """Analyze phase: Run TradingGraph multi-agent analysis on top candidates.

        Invokes TradingGraph's propagate_async() for each top candidate, running
        the full 7-analyst pipeline → bull/bear debate → risk assessment → verdict.
        Results are stored in state.analysis_reports for the Decide phase.
        """
        logger.debug("Task %s O%d: Analyze phase", task_id, state.iteration)

        if not state.candidates:
            logger.info("Task %s: No candidates to analyze", task_id)
            return

        # Take top N candidates (default 3)
        analyze_top_n = 3
        if config.dag:
            for dag_task in config.dag.get("tasks", []):
                if dag_task.get("action") == "select":
                    params = dag_task.get("params", {})
                    analyze_top_n = params.get("analyze_top_n", 3)
                    break

        top_candidates = state.candidates[:analyze_top_n]
        total = len(top_candidates)
        logger.info(
            "Task %s: Analyzing %d candidates with TradingGraph", task_id, total
        )

        # Lazy import to avoid circular deps
        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph
        except ImportError:
            logger.warning("TradingAgentsGraph not available — skipping analyze phase")
            return

        today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for i, candidate in enumerate(top_candidates, 1):
            symbol = candidate.get("symbol", "")
            if not symbol:
                continue

            logger.info(
                "Analyzing %s with TradingGraph (%d/%d)", symbol, i, total
            )

            try:
                # Build config reusing the loop's LLM settings
                graph_config = {
                    "llm_provider": self._router._provider,
                    "backend_url": self._router._base_url,
                    "quick_think_llm": self._router._model,
                    "deep_think_llm": self._router._model,
                    "project_dir": os.path.join(os.path.dirname(__file__), "../../"),
                    "max_debate_rounds": 1,
                    "max_risk_discuss_rounds": 1,
                }
                graph = TradingAgentsGraph(config=graph_config)

                # propagate_async runs: data collection → 7 analysts →
                # bull/bear debate → risk assessment → final decision
                result = await graph.propagate_async(symbol, today_date)

                # Extract signal from final trade decision
                short_term = result.get("short_term", {})
                final_decision = short_term.get("final_trade_decision", "")
                signal = graph.process_signal(final_decision)

                # Extract structured verdict data (direction, confidence, risk_flags)
                from tradingagents.graph.signal_processing import extract_verdict_data
                verdict_data = extract_verdict_data(final_decision)
                confidence = verdict_data.get("confidence", 0.5)

                report = {
                    "symbol": symbol,
                    "verdict": signal,  # BUY / SELL / HOLD
                    "confidence": confidence,
                    "final_trade_decision": final_decision[:500],
                    "market_report": short_term.get("market_report", "")[:300],
                    "sentiment_report": short_term.get("sentiment_report", "")[:300],
                    "news_report": short_term.get("news_report", "")[:300],
                    "fundamentals_report": short_term.get("fundamentals_report", "")[:300],
                    "macro_report": short_term.get("macro_report", "")[:300],
                    "smart_money_report": short_term.get("smart_money_report", "")[:300],
                    "volume_price_report": short_term.get("volume_price_report", "")[:300],
                    "verdict_data": verdict_data,
                }
                state.analysis_reports.append(report)
                self._save_analysis_report(task_id, state.iteration, report)

                logger.info(
                    "Task %s: %s → %s (confidence=%.2f)",
                    task_id, symbol, signal, confidence,
                )

            except Exception as e:
                logger.error(
                    "Task %s: TradingGraph failed for %s: %s",
                    task_id, symbol, e, exc_info=True,
                )
                error_report = {
                    "symbol": symbol,
                    "verdict": "HOLD",
                    "confidence": 0.0,
                    "error": str(e),
                }
                state.analysis_reports.append(error_report)
                self._save_analysis_report(task_id, state.iteration, error_report)

    def _phase_decide(
        self, task_id: str, config: AutonomousTaskConfig, state: OODAState
    ) -> None:
        """Decide phase: Allocate capital and plan trades."""
        logger.debug("Task %s O%d: Decide phase", task_id, state.iteration)

        if not state.candidates:
            logger.info("Task %s: No candidates to allocate", task_id)
            return

        # ── Disconnection #4: Merge TradingGraph verdicts into candidates ──
        if state.analysis_reports:
            verdict_map = {
                r["symbol"]: r for r in state.analysis_reports if r.get("symbol")
            }
            adjusted = []
            for c in state.candidates:
                sym = c.get("symbol", "")
                report = verdict_map.get(sym)
                if report:
                    c = dict(c)  # Shallow copy to avoid mutating originals
                    verdict = report.get("verdict", "HOLD")
                    confidence = report.get("confidence", 0.0)
                    c["tg_verdict"] = verdict
                    c["tg_confidence"] = confidence

                    if verdict == "BUY" and confidence > 0.7:
                        # High-confidence BUY — boost score by 20%
                        c["composite_score"] = c.get("composite_score", 0.5) * 1.2
                        logger.info(
                            "Task %s: %s BUY@%.2f — boosting score to %.3f",
                            task_id, sym, confidence, c["composite_score"],
                        )
                    elif verdict == "SELL":
                        # SELL — zero out to remove from allocation
                        c["composite_score"] = 0.0
                        logger.info(
                            "Task %s: %s SELL — removing from allocation",
                            task_id, sym,
                        )
                    elif verdict == "HOLD":
                        # HOLD — reduce score by 50%
                        c["composite_score"] = c.get("composite_score", 0.5) * 0.5
                        logger.info(
                            "Task %s: %s HOLD — halving score to %.3f",
                            task_id, sym, c["composite_score"],
                        )
                    # BUY with low confidence: keep original score unchanged
                adjusted.append(c)
            state.candidates = adjusted

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
                        "side": decision["action"],  # buy or sell for equity curve
                        "action_taken": result.action_taken,
                        "order_id": result.order_id,
                        "quantity": result.quantity,
                        "price": result.price,
                        "reason": result.reason,
                    })

                    # ── L7: Auto-reflect on each executed trade ──
                    if result.action_taken in ("buy", "sell"):
                        self._reflect_on_trade(task_id, decision, result)

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
        """Build a time-ordered equity curve tracking real cash flows.

        Buy trades reduce equity (cash outflow to acquire shares),
        sell trades increase equity (cash inflow from liquidation).
        Tasks are sorted by started_at to ensure correct time ordering.

        Returns:
            List of portfolio values over time.
        """
        config_data = self._store.get(task_id)
        if not config_data:
            return []

        # Get starting budget
        meta = config_data.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        budget = meta.get("budget", 10000.0)

        # Collect all executions with timestamps for time-ordered replay
        all_executions: List[Dict[str, Any]] = []

        # Current task's executions
        checkpoint = self._store.get_checkpoint(task_id)
        state_data = checkpoint.get("state", {})
        task_started = config_data.get("started_at", "")
        for ex in state_data.get("executions", []):
            all_executions.append({"ex": ex, "started_at": task_started})

        # Historical completed tasks — sort by started_at for time ordering (W2-1)
        tasks = self._store.list_tasks(status="completed", limit=20)
        tasks_sorted = sorted(tasks, key=lambda t: t.get("started_at", ""))
        for t in tasks_sorted:
            cp = t.get("checkpoint") or {}
            if isinstance(cp, str):
                cp = json.loads(cp)
            s = cp.get("state", {})
            t_started = t.get("started_at", "")
            for ex in s.get("executions", []):
                all_executions.append({"ex": ex, "started_at": t_started})

        # Replay executions: buy reduces equity (cash outflow), sell increases (inflow)
        equity = [budget]
        for item in all_executions:
            ex = item["ex"]
            if ex.get("action_taken") != "executed":
                continue
            price = ex.get("price", 0)
            qty = ex.get("quantity", 0)
            if price <= 0 or qty <= 0:
                continue

            # Infer side from trade decisions in the execution context
            side = ex.get("side", "buy")
            if side == "buy":
                equity.append(equity[-1] - price * qty)  # cash outflow
            else:
                equity.append(equity[-1] + price * qty)   # cash inflow

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

        # W4-1: positions lifted outside task loop for cross-task FIFO matching
        positions: Dict[str, List[List[float]]] = defaultdict(list)

        for task in tasks:
            checkpoint = task.get("checkpoint") or {}
            if isinstance(checkpoint, str):
                checkpoint = json.loads(checkpoint)
            state_data = checkpoint.get("state", {})

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

    # ── Win rate reflection (L-7~8) ──────────────────────────────────────

    def _trigger_reflection_if_needed(
        self, task_id: str, config: AutonomousTaskConfig, state: OODAState
    ) -> None:
        """L-7~8: Trigger SimTradeReflector when win rate drops below 40%.

        Logs a warning and records a reflection event in the state.
        """
        try:
            from tradingagents.dataflows.quant_metrics import QuantMetrics
            returns = self._get_recent_returns()
            if len(returns) < 10:
                return
            wr = QuantMetrics.win_rate(returns)
            if wr < 0.4:
                logger.warning(
                    "Low win rate %.1f%% — triggering reflection",
                    wr * 100,
                )
                # Try to invoke SimTradeReflector if available
                reflector = self._get_reflector()
                if reflector:
                    # W5-1: Pass full trade context for richer reflection
                    all_returns_summary = {
                        "mean": sum(returns) / len(returns) if returns else 0,
                        "min": min(returns) if returns else 0,
                        "max": max(returns) if returns else 0,
                        "count": len(returns),
                    }
                    reflector.reflect_on_sim_trade(
                        trade_info={
                            "reason": "low_win_rate",
                            "win_rate": wr,
                            "total_trades": len(returns),
                        },
                        trade_result={
                            "recent_returns": returns[-10:],
                            "all_returns_summary": all_returns_summary,
                        },
                    )
                    logger.info("SimTradeReflector invoked for low win rate")
                else:
                    logger.info(
                        "SimTradeReflector not available — reflection logged only"
                    )
                # Record in state for checkpointing
                state.errors.append(
                    f"Low win rate {wr*100:.1f}% — reflection triggered"
                )
        except Exception as e:
            logger.warning("Reflection trigger failed: %s", e)

    def _reflect_on_trade(self, task_id: str, decision: Dict[str, Any], result: Any) -> None:
        """L7: Auto-reflect on a completed trade and store lesson in BM25 memory.

        Args:
            task_id: Autonomous task ID
            decision: Trade decision dict {symbol, action, shares, amount, reasoning}
            result: SimExecutor result {action_taken, order_id, quantity, price, reason}
        """
        reflector = self._get_reflector()
        if not reflector:
            return

        try:
            trade_info = {
                "symbol": decision.get("symbol", ""),
                "signal": decision.get("action", "buy"),
                "confidence": 0.7,
                "price": result.price if hasattr(result, "price") else 0.0,
                "quantity": result.quantity if hasattr(result, "quantity") else 0,
                "reasoning": decision.get("reasoning", ""),
                "strategy": self._strategy_params.get("display_name", "") if self._strategy_params else "",
            }
            trade_result = {
                "action_taken": result.action_taken if hasattr(result, "action_taken") else "unknown",
                "order_id": result.order_id if hasattr(result, "order_id") else "",
                "pnl": 0.0,
                "outcome": "pending",
            }
            lesson = reflector.reflect_on_sim_trade(trade_info, trade_result)
            logger.info(
                "Task %s: Reflection stored for %s — %s",
                task_id, decision.get("symbol", ""), lesson[:100],
            )
        except Exception as e:
            logger.debug("Auto-reflection failed (non-critical): %s", e)

    def _get_reflector(self):
        """Get or create a SimTradeReflector instance.

        Returns:
            SimTradeReflector instance, or None if not available.
        """
        try:
            from tradingagents.graph.reflection import SimTradeReflector
            from tradingagents.llm_clients.factory import create_llm_client

            # Use same LLM config as the loop
            client = create_llm_client(
                provider=self._router._provider,
                model=self._router._model,
                base_url=self._router._base_url,
                api_key=self._router._api_key,
            )
            llm = client.get_llm()
            return SimTradeReflector(llm)
        except Exception:
            return None

    def _save_analysis_report(self, task_id: str, iteration: int, report: Dict[str, Any]) -> None:
        """Persist a single analysis report to the analysis_reports DB table.

        Called from _phase_analyze() after each TradingGraph run completes.
        Failures are logged but never block the OODA loop.
        """
        try:
            from api.database import SessionLocal, AnalysisReportDB
            import uuid

            db = SessionLocal()
            try:
                row = AnalysisReportDB(
                    id=uuid.uuid4().hex,
                    task_id=task_id,
                    iteration=iteration,
                    symbol=report.get("symbol", ""),
                    verdict=report.get("verdict", "HOLD"),
                    confidence=report.get("confidence", 0.0),
                    final_trade_decision=report.get("final_trade_decision", ""),
                    market_report=report.get("market_report", ""),
                    sentiment_report=report.get("sentiment_report", ""),
                    news_report=report.get("news_report", ""),
                    fundamentals_report=report.get("fundamentals_report", ""),
                    macro_report=report.get("macro_report", ""),
                    smart_money_report=report.get("smart_money_report", ""),
                    volume_price_report=report.get("volume_price_report", ""),
                    verdict_data=report.get("verdict_data"),
                    error=report.get("error"),
                )
                db.add(row)
                db.commit()
                logger.info(
                    "Task %s/I%d: Persisted analysis report for %s (%s, conf=%.2f)",
                    task_id, iteration, report.get("symbol"), report.get("verdict"), report.get("confidence", 0),
                )
            finally:
                db.close()
        except Exception as e:
            logger.debug("Failed to persist analysis report (non-critical): %s", e)

    def _config_from_dict(self, d: Dict[str, Any]) -> AutonomousTaskConfig:
        """Reconstruct AutonomousTaskConfig from dict."""
        return AutonomousTaskConfig(
            command=d.get("command", ""),
            dag=d.get("dag"),
            budget=d.get("budget", 10000.0),
            currency=d.get("currency", "USD"),
            mode=d.get("mode", "simulate"),
            strategy_name=d.get("strategy_name"),
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
