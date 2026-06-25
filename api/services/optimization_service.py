"""Optimisation service — wraps BF/GA optimisation for the API layer.

Provides a clean interface between the API endpoint and the core optimizer,
handling configuration assembly, signal function construction, and result formatting.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── In-memory job store (mirrors backtest_service pattern) ──────────────────

_opt_jobs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set(job_id: str, **kwargs: Any) -> None:
    with _lock:
        if job_id not in _opt_jobs:
            _opt_jobs[job_id] = {}
        _opt_jobs[job_id].update(kwargs)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _opt_jobs.get(job_id)


def list_jobs() -> List[Dict[str, Any]]:
    with _lock:
        return sorted(
            _opt_jobs.values(),
            key=lambda j: j.get("created_at", ""),
            reverse=True,
        )


def delete_job(job_id: str) -> bool:
    with _lock:
        if job_id in _opt_jobs:
            del _opt_jobs[job_id]
            return True
        return False


# ── Core optimisation runner ────────────────────────────────────────────────

def submit_optimization(
    strategy_name: str,
    parameters: List[Dict[str, Any]],
    target: str = "sharpe_ratio",
    method: str = "bf",
    market: str = "us",
    initial_capital: float = 1_000_000.0,
    n_processes: Optional[int] = None,
    # GA-specific
    population_size: int = 50,
    n_generations: int = 50,
    crossover_rate: float = 0.8,
    mutation_rate: float = 0.1,
    seed: Optional[int] = None,
) -> str:
    """Submit an optimisation job (runs synchronously in a background thread).

    Args:
        strategy_name: Name of the strategy to optimise.
        parameters: List of parameter ranges, each with name/start/end/step.
        target: Target metric (sharpe_ratio, max_drawdown, win_rate, etc.).
        method: "bf" (brute force) or "ga" (genetic algorithm).
        market: "us" or "hk".
        initial_capital: Starting capital for backtest.
        n_processes: Number of parallel workers (BF only).
        population_size: GA population size.
        n_generations: GA generations.
        crossover_rate: GA crossover probability.
        mutation_rate: GA mutation probability.
        seed: Random seed for GA reproducibility.

    Returns:
        Job ID for polling.
    """
    job_id = uuid4().hex[:12]
    _set(
        job_id=job_id,
        status="pending",
        strategy_name=strategy_name,
        target=target,
        method=method,
        created_at=_utcnow_iso(),
    )

    # Run in background thread
    thread = threading.Thread(
        target=_run_optimization,
        args=(job_id, strategy_name, parameters, target, method,
              market, initial_capital, n_processes,
              population_size, n_generations, crossover_rate, mutation_rate, seed),
        daemon=True,
    )
    thread.start()
    return job_id


def _run_optimization(
    job_id: str,
    strategy_name: str,
    parameters: List[Dict[str, Any]],
    target: str,
    method: str,
    market: str,
    initial_capital: float,
    n_processes: Optional[int],
    population_size: int,
    n_generations: int,
    crossover_rate: float,
    mutation_rate: float,
    seed: Optional[int],
) -> None:
    """Execute the optimisation in a background thread."""
    _set(job_id=job_id, status="running", started_at=_utcnow_iso())

    try:
        from tradingagents.optimization import (
            OptimizationSetting,
            run_bf_optimization,
            run_ga_optimization,
        )
        from tradingagents.backtest.global_equity_engine import GlobalEquityEngine

        # Build optimisation setting
        setting = OptimizationSetting()
        for p in parameters:
            setting.add_parameter(
                name=p["name"],
                start=float(p["start"]),
                end=float(p["end"]),
                step=float(p.get("step", 1.0)),
            )
        setting.set_target(target)

        # Engine factory
        def engine_factory(config: dict):
            return GlobalEquityEngine(config, market=market)

        # Generate synthetic data + signal function for the strategy
        data_map, signal_fn = _build_strategy_context(strategy_name, market)

        base_config = {"initial_cash": initial_capital}

        # Progress tracking
        progress = {"completed": 0, "total": 0}

        def on_progress_bf(completed: int, total: int) -> None:
            progress["completed"] = completed
            progress["total"] = total
            _set(
                job_id=job_id,
                progress={"completed": completed, "total": total},
            )

        def on_progress_ga(gen: int, total_gens: int, best: float) -> None:
            _set(
                job_id=job_id,
                progress={"generation": gen, "total_generations": total_gens, "best_fitness": best},
            )

        if method == "bf":
            results = run_bf_optimization(
                setting=setting,
                engine_factory=engine_factory,
                data_map=data_map,
                signal_fn=signal_fn,
                base_config=base_config,
                n_processes=n_processes,
                progress_callback=on_progress_bf,
            )
        elif method == "ga":
            results = run_ga_optimization(
                setting=setting,
                engine_factory=engine_factory,
                data_map=data_map,
                signal_fn=signal_fn,
                base_config=base_config,
                population_size=population_size,
                n_generations=n_generations,
                crossover_rate=crossover_rate,
                mutation_rate=mutation_rate,
                seed=seed,
                progress_callback=on_progress_ga,
            )
        else:
            raise ValueError(f"Unknown method: {method}. Use 'bf' or 'ga'.")

        # Format results
        formatted = []
        for r in results[:100]:  # Top 100 results
            formatted.append({
                "params": r.params,
                "target_value": round(r.target_value, 6),
                "metrics": {
                    k: round(v, 6) if isinstance(v, float) else v
                    for k, v in r.metrics.items()
                },
            })

        _set(
            job_id=job_id,
            status="completed",
            completed_at=_utcnow_iso(),
            best_params=results[0].params if results else {},
            best_target_value=round(results[0].target_value, 6) if results else 0,
            best_metrics={
                k: round(v, 6) if isinstance(v, float) else v
                for k, v in (results[0].metrics if results else {}).items()
            },
            results=formatted,
            total_evaluated=len(formatted),
        )

    except Exception as e:
        logger.error("Optimisation job %s failed: %s", job_id, e, exc_info=True)
        _set(
            job_id=job_id,
            status="failed",
            error=str(e),
            completed_at=_utcnow_iso(),
        )


def _build_strategy_context(
    strategy_name: str,
    market: str,
) -> tuple:
    """Build data_map and signal_fn for a given strategy.

    Returns:
        (data_map, signal_fn) where signal_fn(params, data_map) -> signal_map
    """
    # Generate synthetic OHLCV data for backtesting
    # In production this would load real data from FutuProvider
    dates = pd.date_range("2024-01-01", periods=252, freq="B")
    rng = np.random.default_rng(42)

    # Generate realistic price series
    base_price = 100.0
    returns = rng.normal(0.0003, 0.015, len(dates))
    prices = base_price * np.cumprod(1 + returns)

    data_map = {
        f"{'HK.00700' if market == 'hk' else 'US.AAPL'}": pd.DataFrame({
            "open": prices * (1 + rng.uniform(-0.005, 0.005, len(dates))),
            "high": prices * (1 + rng.uniform(0.001, 0.02, len(dates))),
            "low": prices * (1 - rng.uniform(0.001, 0.02, len(dates))),
            "close": prices,
            "volume": rng.integers(10000, 1000000, len(dates)),
        }, index=dates),
    }

    def signal_fn(params: dict, data_map: dict) -> dict:
        """Generate trading signals based on strategy parameters."""
        signals = {}
        for sym, df in data_map.items():
            close = df["close"]
            # Default: momentum strategy with configurable MA periods
            fast_period = int(params.get("fast_period", 10))
            slow_period = int(params.get("slow_period", 30))
            threshold = float(params.get("threshold", 0.0))

            fast_ma = close.rolling(fast_period).mean()
            slow_ma = close.rolling(slow_period).mean()

            # Signal: fast > slow → long, else flat
            raw_signal = pd.Series(0.0, index=close.index)
            raw_signal[fast_ma > slow_ma * (1 + threshold)] = 1.0
            raw_signal[fast_ma < slow_ma * (1 - threshold)] = -1.0

            signals[sym] = raw_signal.fillna(0.0)

        return signals

    return data_map, signal_fn
