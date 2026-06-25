"""Parameter optimiser for TradingAgents-Futu backtests.

Ported from vnpy trader/optimize.py with TAF-specific adaptations:
  - Uses TAF's BaseEngine.run() pipeline (align → execute → metrics)
  - GA implemented with numpy only (no DEAP dependency)
  - BF supports multiprocessing.Pool for parallel grid search
  - Target metrics: sharpe_ratio, max_drawdown, win_rate, total_return, calmar

Usage:
    from tradingagents.optimization import OptimizationSetting, run_bf_optimization, run_ga_optimization

    setting = OptimizationSetting()
    setting.add_parameter("fast_period", 5, 20, 1)
    setting.add_parameter("slow_period", 20, 60, 5)
    setting.set_target("sharpe_ratio")

    # Brute force
    results = run_bf_optimization(setting, engine, data_map, signal_fn, n_processes=4)

    # Genetic algorithm
    results = run_ga_optimization(setting, engine, data_map, signal_fn, n_generations=50)
"""

from __future__ import annotations

import copy
import itertools
import logging
import multiprocessing
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Target metric names (keys in calc_metrics output) ───────────────────────

VALID_TARGETS = {
    "sharpe_ratio",
    "max_drawdown",
    "win_rate",
    "total_return",
    "annual_return",
    "calmar",
    "sortino",
    "profit_factor",
}

# Metrics where HIGHER is better (used by GA fitness)
_HIGHER_IS_BETTER = {
    "sharpe_ratio",
    "win_rate",
    "total_return",
    "annual_return",
    "calmar",
    "sortino",
    "profit_factor",
}

# Metrics where LOWER is better
_LOWER_IS_BETTER = {
    "max_drawdown",
}


# ── Data models ─────────────────────────────────────────────────────────────

@dataclass
class ParameterRange:
    """A single optimisable parameter with its search range.

    Attributes:
        name: Parameter name (must match a key in the strategy config dict).
        start: Lower bound (inclusive).
        end: Upper bound (inclusive).
        step: Step size for grid search. GA ignores this and uses continuous range.
    """

    name: str
    start: float
    end: float
    step: float

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError(
                f"Parameter '{self.name}': start ({self.start}) must be < end ({self.end})"
            )
        if self.step <= 0:
            raise ValueError(
                f"Parameter '{self.name}': step ({self.step}) must be > 0"
            )

    def grid_values(self) -> List[float]:
        """Generate discrete values for brute-force grid search."""
        values = []
        v = self.start
        while v <= self.end + 1e-10:  # floating-point tolerance
            values.append(round(v, 10))
            v += self.step
        return values

    def random_value(self, rng: np.random.Generator) -> float:
        """Sample a random value uniformly from [start, end]."""
        return float(rng.uniform(self.start, self.end))


@dataclass
class OptimizationResult:
    """Result of a single parameter combination evaluation.

    Attributes:
        params: Parameter values used for this run.
        target_value: Value of the optimisation target metric.
        metrics: Full metrics dict from the backtest run.
    """

    params: Dict[str, float]
    target_value: float
    metrics: Dict[str, Any]


# ── OptimizationSetting ─────────────────────────────────────────────────────

class OptimizationSetting:
    """Configure an optimisation run: parameters to search and target metric.

    Example:
        setting = OptimizationSetting()
        setting.add_parameter("fast_ma", 5, 20, 1)
        setting.add_parameter("slow_ma", 20, 60, 5)
        setting.set_target("sharpe_ratio")
    """

    def __init__(self) -> None:
        self._parameters: List[ParameterRange] = []
        self._target: str = "sharpe_ratio"

    def add_parameter(
        self,
        name: str,
        start: float,
        end: float,
        step: float = 1.0,
    ) -> None:
        """Add a parameter range to search.

        Args:
            name: Parameter name (key in strategy config dict).
            start: Lower bound (inclusive).
            end: Upper bound (inclusive).
            step: Step size for grid search.

        Raises:
            ValueError: If parameter name is already registered.
        """
        for p in self._parameters:
            if p.name == name:
                raise ValueError(f"Parameter '{name}' already registered")
        self._parameters.append(ParameterRange(name, start, end, step))

    def set_target(self, target_name: str) -> None:
        """Set the optimisation target metric.

        Args:
            target_name: One of sharpe_ratio, max_drawdown, win_rate,
                         total_return, annual_return, calmar, sortino, profit_factor.

        Raises:
            ValueError: If target_name is not a recognised metric.
        """
        # Normalise legacy names
        if target_name == "sharpe":
            target_name = "sharpe_ratio"
        if target_name not in VALID_TARGETS:
            raise ValueError(
                f"Unknown target '{target_name}'. Valid: {sorted(VALID_TARGETS)}"
            )
        self._target = target_name

    @property
    def target(self) -> str:
        return self._target

    @property
    def parameters(self) -> List[ParameterRange]:
        return list(self._parameters)

    def generate_settings(self) -> List[Dict[str, float]]:
        """Generate all parameter combinations for brute-force search.

        Returns:
            List of dicts, each mapping parameter name → value.
        """
        if not self._parameters:
            return [{}]

        names = [p.name for p in self._parameters]
        grids = [p.grid_values() for p in self._parameters]

        combinations = []
        for combo in itertools.product(*grids):
            combinations.append(dict(zip(names, combo)))

        return combinations

    @property
    def grid_size(self) -> int:
        """Total number of combinations in brute-force search."""
        if not self._parameters:
            return 1
        size = 1
        for p in self._parameters:
            size *= len(p.grid_values())
        return size


# ── Helper: evaluate a single parameter set ─────────────────────────────────

def _evaluate_params(
    params: Dict[str, float],
    base_config: Dict[str, Any],
    engine_factory: Callable,
    data_map: Dict[str, pd.DataFrame],
    signal_fn: Callable[[Dict[str, float], Dict[str, pd.DataFrame]], Dict[str, pd.Series]],
    target: str,
    bars_per_year: int,
) -> Tuple[Dict[str, float], float, Dict[str, Any]]:
    """Run one backtest with the given parameters and return (params, target_value, metrics).

    This is a module-level function so it can be pickled for multiprocessing.
    """
    config = {**base_config, **params}
    engine = engine_factory(config)
    signal_map = signal_fn(params, data_map)
    metrics = engine.run(config, data_map, signal_map, bars_per_year=bars_per_year)

    target_key = target.replace("sharpe_ratio", "sharpe")
    target_value = float(metrics.get(target_key, 0.0))
    return params, target_value, metrics


# ── Brute-force optimisation ────────────────────────────────────────────────

def run_bf_optimization(
    setting: OptimizationSetting,
    engine_factory: Callable,
    data_map: Dict[str, pd.DataFrame],
    signal_fn: Callable[[Dict[str, float], Dict[str, pd.DataFrame]], Dict[str, pd.Series]],
    base_config: Optional[Dict[str, Any]] = None,
    bars_per_year: int = 252,
    n_processes: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> List[OptimizationResult]:
    """Brute-force (exhaustive grid) search over all parameter combinations.

    Uses multiprocessing.Pool for parallel backtests.

    Args:
        setting: Optimisation configuration (parameters + target).
        engine_factory: ``(config) -> BaseEngine`` — creates a fresh engine per run.
        data_map: code → OHLCV DataFrame (shared across all runs).
        signal_fn: ``(params, data_map) → signal_map`` — generates signals
                   for the given parameter values.
        base_config: Base backtest config (parameters are merged on top).
        bars_per_year: Annualisation factor for metrics.
        n_processes: Number of worker processes. None = cpu_count().
        progress_callback: Optional ``(completed, total)`` callback for progress.

    Returns:
        List of OptimizationResult sorted by target value (best first).
    """
    if base_config is None:
        base_config = {}

    all_settings = setting.generate_settings()
    total = len(all_settings)
    target = setting.target

    logger.info(
        "BF optimisation: %d combinations, target=%s, processes=%s",
        total, target, n_processes or "auto",
    )

    if total == 1:
        # Single combination — no need for multiprocessing overhead
        params, target_value, metrics = _evaluate_params(
            all_settings[0], base_config, engine_factory,
            data_map, signal_fn, target, bars_per_year,
        )
        result = OptimizationResult(params=params, target_value=target_value, metrics=metrics)
        if progress_callback:
            progress_callback(1, 1)
        return [result]

    # Prepare worker arguments
    worker_args = [
        (params, base_config, engine_factory, data_map, signal_fn, target, bars_per_year)
        for params in all_settings
    ]

    results: List[OptimizationResult] = []
    is_higher_better = target in _HIGHER_IS_BETTER

    with multiprocessing.Pool(processes=n_processes) as pool:
        for i, (params, target_value, metrics) in enumerate(
            pool.starmap(_evaluate_params, worker_args)
        ):
            results.append(
                OptimizationResult(params=params, target_value=target_value, metrics=metrics)
            )
            if progress_callback:
                progress_callback(i + 1, total)

    # Sort: best first
    results.sort(
        key=lambda r: r.target_value,
        reverse=is_higher_better,
    )

    logger.info(
        "BF optimisation complete. Best: target=%.6f, params=%s",
        results[0].target_value if results else 0,
        results[0].params if results else {},
    )

    return results


# ── Genetic Algorithm ───────────────────────────────────────────────────────

# GA hyperparameters (sensible defaults, not tunable by user)
_GA_DEFAULTS = {
    "population_size": 50,
    "n_generations": 50,
    "crossover_rate": 0.8,
    "mutation_rate": 0.1,
    "tournament_size": 3,
    "elitism_count": 2,
}


class _GAIndividual:
    """A single individual in the GA population."""

    __slots__ = ("genes", "fitness", "metrics")

    def __init__(
        self,
        genes: Dict[str, float],
        fitness: float = float("-inf"),
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.genes = genes
        self.fitness = fitness
        self.metrics = metrics


def run_ga_optimization(
    setting: OptimizationSetting,
    engine_factory: Callable,
    data_map: Dict[str, pd.DataFrame],
    signal_fn: Callable[[Dict[str, float], Dict[str, pd.DataFrame]], Dict[str, pd.Series]],
    base_config: Optional[Dict[str, Any]] = None,
    bars_per_year: int = 252,
    population_size: int = 50,
    n_generations: int = 50,
    crossover_rate: float = 0.8,
    mutation_rate: float = 0.1,
    seed: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int, float], None]] = None,
) -> List[OptimizationResult]:
    """Genetic algorithm search over parameter space.

    Uses numpy for all random operations — no DEAP dependency.

    Args:
        setting: Optimisation configuration (parameters + target).
        engine_factory: ``(config) -> BaseEngine``.
        data_map: code → OHLCV DataFrame.
        signal_fn: ``(params, data_map) → signal_map``.
        base_config: Base backtest config.
        bars_per_year: Annualisation factor.
        population_size: Number of individuals per generation.
        n_generations: Number of generations to evolve.
        crossover_rate: Probability of crossover (vs cloning parent).
        mutation_rate: Probability of mutating each gene.
        seed: Random seed for reproducibility.
        progress_callback: Optional ``(generation, n_generations, best_fitness)`` callback.

    Returns:
        List of OptimizationResult sorted by target value (best first).
    """
    if base_config is None:
        base_config = {}

    if not setting.parameters:
        # No parameters to optimise — just run once
        params, target_value, metrics = _evaluate_params(
            {}, base_config, engine_factory,
            data_map, signal_fn, setting.target, bars_per_year,
        )
        return [OptimizationResult(params=params, target_value=target_value, metrics=metrics)]

    rng = np.random.default_rng(seed)
    target = setting.target
    is_higher_better = target in _HIGHER_IS_BETTER
    param_ranges = setting.parameters

    # ── Initialise population ───────────────────────────────────────────────
    population: List[_GAIndividual] = []
    for _ in range(population_size):
        genes = {}
        for pr in param_ranges:
            genes[pr.name] = pr.random_value(rng)
        population.append(_GAIndividual(genes=genes))

    # ── Evaluate initial population ─────────────────────────────────────────
    for ind in population:
        _, ind.fitness, ind.metrics = _evaluate_params(
            ind.genes, base_config, engine_factory,
            data_map, signal_fn, target, bars_per_year,
        )

    best_ever = max(population, key=lambda i: i.fitness) if is_higher_better \
        else min(population, key=lambda i: i.fitness)

    logger.info(
        "GA initialisation: pop=%d, best_fitness=%.6f",
        population_size, best_ever.fitness,
    )

    # ── Evolution loop ──────────────────────────────────────────────────────
    for gen in range(n_generations):
        new_population: List[_GAIndividual] = []

        # Elitism: carry forward top N unchanged
        sorted_pop = sorted(
            population,
            key=lambda i: i.fitness,
            reverse=is_higher_better,
        )
        for i in range(min(_GA_DEFAULTS["elitism_count"], len(sorted_pop))):
            elite = _GAIndividual(
                genes=dict(sorted_pop[i].genes),
                fitness=sorted_pop[i].fitness,
                metrics=sorted_pop[i].metrics,
            )
            new_population.append(elite)

        # Fill rest via selection + crossover + mutation
        while len(new_population) < population_size:
            parent1 = _tournament_select(population, rng, is_higher_better)
            parent2 = _tournament_select(population, rng, is_higher_better)

            if rng.random() < crossover_rate:
                child_genes = _crossover(parent1.genes, parent2.genes, param_ranges, rng)
            else:
                child_genes = dict(parent1.genes)

            _mutate(child_genes, param_ranges, mutation_rate, rng)
            new_population.append(_GAIndividual(genes=child_genes))

        # Evaluate new individuals (skip elites which already have fitness)
        for ind in new_population:
            if ind.metrics is not None:
                continue  # elite — already evaluated
            _, ind.fitness, ind.metrics = _evaluate_params(
                ind.genes, base_config, engine_factory,
                data_map, signal_fn, target, bars_per_year,
            )

        population = new_population

        # Track best ever
        gen_best = max(population, key=lambda i: i.fitness) if is_higher_better \
            else min(population, key=lambda i: i.fitness)
        if is_higher_better:
            if gen_best.fitness > best_ever.fitness:
                best_ever = _GAIndividual(
                    genes=dict(gen_best.genes),
                    fitness=gen_best.fitness,
                    metrics=gen_best.metrics,
                )
        else:
            if gen_best.fitness < best_ever.fitness:
                best_ever = _GAIndividual(
                    genes=dict(gen_best.genes),
                    fitness=gen_best.fitness,
                    metrics=gen_best.metrics,
                )

        if progress_callback:
            progress_callback(gen + 1, n_generations, best_ever.fitness)

        logger.debug(
            "GA gen %d/%d: best=%.6f, ever=%.6f",
            gen + 1, n_generations, gen_best.fitness, best_ever.fitness,
        )

    # ── Collect results ─────────────────────────────────────────────────────
    results = []
    seen = set()
    for ind in population:
        key = tuple(sorted(ind.genes.items()))
        if key in seen:
            continue
        seen.add(key)
        results.append(
            OptimizationResult(
                params=ind.genes,
                target_value=ind.fitness,
                metrics=ind.metrics or {},
            )
        )

    results.sort(key=lambda r: r.target_value, reverse=is_higher_better)

    logger.info(
        "GA complete: %d unique results, best=%.6f, params=%s",
        len(results), results[0].target_value if results else 0,
        results[0].params if results else {},
    )

    return results


# ── GA operators ────────────────────────────────────────────────────────────

def _tournament_select(
    population: List[_GAIndividual],
    rng: np.random.Generator,
    is_higher_better: bool,
) -> _GAIndividual:
    """Tournament selection: pick the best from a random subset."""
    tournament_size = min(_GA_DEFAULTS["tournament_size"], len(population))
    indices = rng.choice(len(population), size=tournament_size, replace=False)
    candidates = [population[i] for i in indices]
    if is_higher_better:
        return max(candidates, key=lambda i: i.fitness)
    return min(candidates, key=lambda i: i.fitness)


def _crossover(
    parent1_genes: Dict[str, float],
    parent2_genes: Dict[str, float],
    param_ranges: List[ParameterRange],
    rng: np.random.Generator,
) -> Dict[str, float]:
    """Uniform crossover: each gene randomly from one parent."""
    child = {}
    for pr in param_ranges:
        if rng.random() < 0.5:
            child[pr.name] = parent1_genes[pr.name]
        else:
            child[pr.name] = parent2_genes[pr.name]
    return child


def _mutate(
    genes: Dict[str, float],
    param_ranges: List[ParameterRange],
    mutation_rate: float,
    rng: np.random.Generator,
) -> None:
    """Gaussian mutation: perturb each gene with probability mutation_rate."""
    for pr in param_ranges:
        if rng.random() < mutation_rate:
            # Perturbation: ±10% of range
            range_width = pr.end - pr.start
            sigma = range_width * 0.1
            new_val = genes[pr.name] + rng.normal(0, sigma)
            # Clamp to valid range
            genes[pr.name] = float(np.clip(new_val, pr.start, pr.end))
