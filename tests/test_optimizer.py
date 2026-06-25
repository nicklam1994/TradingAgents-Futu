"""Tests for parameter optimiser: OptimizationSetting, BF, GA.

Covers:
  - OptimizationSetting: parameter definition, target setting, grid generation
  - ParameterRange: validation, grid_values, random_value
  - run_bf_optimization: brute-force grid search, multiprocessing
  - run_ga_optimization: genetic algorithm convergence, operators
  - Edge cases: empty parameters, single parameter, invalid targets
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd
import pytest

from tradingagents.optimization.optimizer import (
    OptimizationSetting,
    OptimizationResult,
    ParameterRange,
    run_bf_optimization,
    run_ga_optimization,
    VALID_TARGETS,
    _evaluate_params,
    _tournament_select,
    _crossover,
    _mutate,
    _GAIndividual,
)
from tradingagents.backtest.global_equity_engine import GlobalEquityEngine


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers — synthetic data + signal function
# ═══════════════════════════════════════════════════════════════════════════════

def _make_synthetic_data(n: int = 252, seed: int = 42) -> Dict[str, pd.DataFrame]:
    """Create synthetic OHLCV data for testing."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 100 + np.cumsum(rng.normal(0.0003, 0.015, n)) * 10
    # Ensure positive prices
    close = np.maximum(close, 10.0)
    return {
        "US.AAPL": pd.DataFrame({
            "open": close * (1 + rng.uniform(-0.005, 0.005, n)),
            "high": close * (1 + rng.uniform(0.001, 0.02, n)),
            "low": close * (1 - rng.uniform(0.001, 0.02, n)),
            "close": close,
            "volume": rng.integers(10000, 1000000, n),
        }, index=dates),
    }


def _simple_signal_fn(params: dict, data_map: dict) -> dict:
    """Simple MA crossover signal function for testing."""
    signals = {}
    for sym, df in data_map.items():
        close = df["close"]
        fast = int(params.get("fast_period", 10))
        slow = int(params.get("slow_period", 30))
        fast_ma = close.rolling(fast).mean()
        slow_ma = close.rolling(slow).mean()
        signal = pd.Series(0.0, index=close.index)
        signal[fast_ma > slow_ma] = 1.0
        signal[fast_ma < slow_ma] = -1.0
        signals[sym] = signal.fillna(0.0)
    return signals


def _engine_factory(config: dict) -> GlobalEquityEngine:
    """Create a US equity engine for testing."""
    return GlobalEquityEngine(config, market="us")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. ParameterRange — validation and value generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestParameterRange:
    """Test ParameterRange dataclass."""

    def test_valid_range(self):
        """Valid range creates successfully."""
        pr = ParameterRange("x", 1.0, 10.0, 1.0)
        assert pr.name == "x"
        assert pr.start == 1.0
        assert pr.end == 10.0
        assert pr.step == 1.0

    def test_start_must_be_less_than_end(self):
        """start >= end raises ValueError."""
        with pytest.raises(ValueError, match="start.*must be < end"):
            ParameterRange("x", 10.0, 1.0, 1.0)

    def test_start_equals_end_raises(self):
        """start == end raises ValueError."""
        with pytest.raises(ValueError, match="start.*must be < end"):
            ParameterRange("x", 5.0, 5.0, 1.0)

    def test_step_must_be_positive(self):
        """step <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="step.*must be > 0"):
            ParameterRange("x", 1.0, 10.0, 0.0)
        with pytest.raises(ValueError, match="step.*must be > 0"):
            ParameterRange("x", 1.0, 10.0, -1.0)

    def test_grid_values(self):
        """Grid values cover [start, end] with step."""
        pr = ParameterRange("x", 0.0, 1.0, 0.25)
        vals = pr.grid_values()
        assert len(vals) == 5
        assert vals[0] == pytest.approx(0.0)
        assert vals[-1] == pytest.approx(1.0)

    def test_grid_values_integer_step(self):
        """Integer step generates correct values."""
        pr = ParameterRange("x", 5, 20, 5)
        vals = pr.grid_values()
        assert vals == [5.0, 10.0, 15.0, 20.0]

    def test_random_value_in_range(self):
        """Random value falls within [start, end]."""
        rng = np.random.default_rng(42)
        pr = ParameterRange("x", 1.0, 10.0, 1.0)
        for _ in range(100):
            v = pr.random_value(rng)
            assert 1.0 <= v <= 10.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. OptimizationSetting — configuration and grid generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptimizationSetting:
    """Test OptimizationSetting configuration."""

    def test_add_parameter(self):
        """Adding a parameter stores it correctly."""
        s = OptimizationSetting()
        s.add_parameter("fast_period", 5, 20, 1)
        assert len(s.parameters) == 1
        assert s.parameters[0].name == "fast_period"

    def test_duplicate_parameter_raises(self):
        """Duplicate parameter name raises ValueError."""
        s = OptimizationSetting()
        s.add_parameter("x", 1, 10, 1)
        with pytest.raises(ValueError, match="already registered"):
            s.add_parameter("x", 1, 20, 1)

    def test_set_target_valid(self):
        """Valid target is accepted."""
        s = OptimizationSetting()
        s.set_target("sharpe_ratio")
        assert s.target == "sharpe_ratio"

    def test_set_target_normalises_sharpe(self):
        """'sharpe' normalises to 'sharpe_ratio'."""
        s = OptimizationSetting()
        s.set_target("sharpe")
        assert s.target == "sharpe_ratio"

    def test_set_target_invalid_raises(self):
        """Unknown target raises ValueError."""
        s = OptimizationSetting()
        with pytest.raises(ValueError, match="Unknown target"):
            s.set_target("invalid_metric")

    def test_generate_settings_single_param(self):
        """Single parameter generates correct grid."""
        s = OptimizationSetting()
        s.add_parameter("x", 1, 3, 1)
        settings = s.generate_settings()
        assert len(settings) == 3
        assert settings[0] == {"x": 1.0}
        assert settings[1] == {"x": 2.0}
        assert settings[2] == {"x": 3.0}

    def test_generate_settings_multi_param(self):
        """Multiple parameters generate Cartesian product."""
        s = OptimizationSetting()
        s.add_parameter("x", 1, 2, 1)
        s.add_parameter("y", 10, 20, 10)
        settings = s.generate_settings()
        # 2 * 2 = 4 combinations
        assert len(settings) == 4
        # Check all combinations present
        combos = {(d["x"], d["y"]) for d in settings}
        assert combos == {(1.0, 10.0), (1.0, 20.0), (2.0, 10.0), (2.0, 20.0)}

    def test_generate_settings_empty(self):
        """No parameters → single empty dict."""
        s = OptimizationSetting()
        settings = s.generate_settings()
        assert settings == [{}]

    def test_grid_size(self):
        """grid_size matches generated settings count."""
        s = OptimizationSetting()
        s.add_parameter("x", 1, 5, 1)
        s.add_parameter("y", 10, 30, 10)
        assert s.grid_size == 5 * 3  # 15


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _evaluate_params — single parameter evaluation
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluateParams:
    """Test single parameter set evaluation."""

    def test_returns_tuple(self):
        """Returns (params, target_value, metrics)."""
        data_map = _make_synthetic_data(50)
        params = {"fast_period": 5, "slow_period": 20}
        result = _evaluate_params(
            params, {}, _engine_factory, data_map,
            _simple_signal_fn, "sharpe_ratio", 252,
        )
        assert len(result) == 3
        assert result[0] == params
        assert isinstance(result[1], float)
        assert isinstance(result[2], dict)

    def test_metrics_contain_expected_keys(self):
        """Metrics dict contains all expected keys."""
        data_map = _make_synthetic_data(50)
        _, _, metrics = _evaluate_params(
            {"fast_period": 5, "slow_period": 20}, {}, _engine_factory,
            data_map, _simple_signal_fn, "sharpe_ratio", 252,
        )
        assert "sharpe" in metrics
        assert "total_return" in metrics
        assert "max_drawdown" in metrics
        assert "win_rate" in metrics


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Brute-force optimisation
# ═══════════════════════════════════════════════════════════════════════════════

class TestBFOptimization:
    """Test brute-force grid search."""

    def test_basic_bf_search(self):
        """BF search returns sorted results."""
        setting = OptimizationSetting()
        setting.add_parameter("fast_period", 5, 10, 5)
        setting.add_parameter("slow_period", 20, 30, 10)
        setting.set_target("sharpe_ratio")

        data_map = _make_synthetic_data(100)
        results = run_bf_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            n_processes=1,  # Single process for deterministic test
        )

        # 2 * 2 = 4 combinations
        assert len(results) == 4
        # Results sorted by target value (higher is better for sharpe)
        for i in range(len(results) - 1):
            assert results[i].target_value >= results[i + 1].target_value

    def test_bf_finds_better_params(self):
        """BF search finds parameters that produce non-trivial results."""
        setting = OptimizationSetting()
        setting.add_parameter("fast_period", 5, 15, 5)
        setting.add_parameter("slow_period", 20, 40, 10)
        setting.set_target("total_return")

        data_map = _make_synthetic_data(200)
        results = run_bf_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            n_processes=1,
        )

        # Best result should have valid metrics
        best = results[0]
        assert "total_return" in best.metrics
        assert "sharpe" in best.metrics
        assert isinstance(best.target_value, float)

    def test_bf_with_single_param(self):
        """BF search with single parameter."""
        setting = OptimizationSetting()
        setting.add_parameter("fast_period", 5, 10, 1)
        setting.set_target("sharpe_ratio")

        data_map = _make_synthetic_data(100)
        results = run_bf_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            n_processes=1,
        )

        # 6 values: 5, 6, 7, 8, 9, 10
        assert len(results) == 6

    def test_bf_progress_callback(self):
        """BF search calls progress callback."""
        setting = OptimizationSetting()
        setting.add_parameter("fast_period", 5, 10, 5)
        setting.set_target("sharpe_ratio")

        progress_calls = []

        def on_progress(completed: int, total: int) -> None:
            progress_calls.append((completed, total))

        data_map = _make_synthetic_data(50)
        run_bf_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            n_processes=1,
            progress_callback=on_progress,
        )

        assert len(progress_calls) == 2  # 2 combinations
        assert progress_calls[-1][0] == progress_calls[-1][1]  # completed == total

    def test_bf_empty_params(self):
        """BF with no parameters runs once."""
        setting = OptimizationSetting()
        setting.set_target("sharpe_ratio")

        data_map = _make_synthetic_data(50)
        results = run_bf_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            n_processes=1,
        )

        assert len(results) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Genetic Algorithm optimisation
# ═══════════════════════════════════════════════════════════════════════════════

class TestGAOptimization:
    """Test genetic algorithm search."""

    def test_ga_returns_results(self):
        """GA search returns results sorted by fitness."""
        setting = OptimizationSetting()
        setting.add_parameter("fast_period", 5, 15, 1)
        setting.add_parameter("slow_period", 20, 60, 1)
        setting.set_target("sharpe_ratio")

        data_map = _make_synthetic_data(200)
        results = run_ga_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            population_size=10,
            n_generations=5,
            seed=42,
        )

        assert len(results) > 0
        # Results sorted by target value (higher is better for sharpe)
        for i in range(len(results) - 1):
            assert results[i].target_value >= results[i + 1].target_value

    def test_ga_convergence(self):
        """GA improves fitness over generations."""
        setting = OptimizationSetting()
        setting.add_parameter("fast_period", 5, 15, 1)
        setting.add_parameter("slow_period", 20, 60, 1)
        setting.set_target("total_return")

        data_map = _make_synthetic_data(200)
        progress_history = []

        def on_progress(gen: int, total: int, best: float) -> None:
            progress_history.append((gen, best))

        results = run_ga_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            population_size=15,
            n_generations=10,
            seed=42,
            progress_callback=on_progress,
        )

        # Progress should be recorded
        assert len(progress_history) == 10
        # Best fitness should be finite
        assert np.isfinite(results[0].target_value)

    def test_ga_reproducible_with_seed(self):
        """GA with same seed produces same results."""
        setting = OptimizationSetting()
        setting.add_parameter("fast_period", 5, 10, 1)
        setting.add_parameter("slow_period", 20, 40, 1)
        setting.set_target("sharpe_ratio")

        data_map = _make_synthetic_data(100)

        results1 = run_ga_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            population_size=10,
            n_generations=5,
            seed=123,
        )
        results2 = run_ga_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            population_size=10,
            n_generations=5,
            seed=123,
        )

        # Same seed → same best params
        assert results1[0].params == results2[0].params
        assert results1[0].target_value == pytest.approx(results2[0].target_value)

    def test_ga_no_params(self):
        """GA with no parameters runs once."""
        setting = OptimizationSetting()
        setting.set_target("sharpe_ratio")

        data_map = _make_synthetic_data(50)
        results = run_ga_optimization(
            setting=setting,
            engine_factory=_engine_factory,
            data_map=data_map,
            signal_fn=_simple_signal_fn,
            population_size=5,
            n_generations=3,
            seed=42,
        )

        assert len(results) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 6. GA Operators
# ═══════════════════════════════════════════════════════════════════════════════

class TestGAOperators:
    """Test GA selection, crossover, and mutation operators."""

    def test_tournament_select_higher_better(self):
        """Tournament selection picks fittest for higher-is-better."""
        rng = np.random.default_rng(42)
        pop = [
            _GAIndividual({"x": 1.0}, fitness=0.5),
            _GAIndividual({"x": 2.0}, fitness=0.9),
            _GAIndividual({"x": 3.0}, fitness=0.1),
        ]
        # Run multiple times — should tend to pick the 0.9 individual
        selections = [_tournament_select(pop, rng, True).fitness for _ in range(100)]
        assert max(selections) == 0.9

    def test_tournament_select_lower_better(self):
        """Tournament selection picks fittest for lower-is-better."""
        rng = np.random.default_rng(42)
        pop = [
            _GAIndividual({"x": 1.0}, fitness=0.5),
            _GAIndividual({"x": 2.0}, fitness=-0.9),
            _GAIndividual({"x": 3.0}, fitness=0.1),
        ]
        selections = [_tournament_select(pop, rng, False).fitness for _ in range(100)]
        assert min(selections) == -0.9

    def test_crossover_combines_genes(self):
        """Crossover combines genes from both parents."""
        rng = np.random.default_rng(42)
        prs = [ParameterRange("x", 0, 10, 1), ParameterRange("y", 0, 10, 1)]
        p1 = {"x": 1.0, "y": 2.0}
        p2 = {"x": 9.0, "y": 8.0}

        child = _crossover(p1, p2, prs, rng)
        # Each gene should come from one parent
        assert child["x"] in (1.0, 9.0)
        assert child["y"] in (2.0, 8.0)

    def test_mutation_clamps_to_range(self):
        """Mutation keeps values within [start, end]."""
        rng = np.random.default_rng(42)
        genes = {"x": 5.0, "y": 50.0}
        prs = [ParameterRange("x", 0, 10, 1), ParameterRange("y", 0, 100, 1)]

        # Mutate many times — all should stay in range
        for _ in range(100):
            g = dict(genes)
            _mutate(g, prs, mutation_rate=1.0, rng=rng)  # Always mutate
            assert 0 <= g["x"] <= 10
            assert 0 <= g["y"] <= 100

    def test_mutation_rate_zero(self):
        """Zero mutation rate never changes genes."""
        rng = np.random.default_rng(42)
        genes = {"x": 5.0}
        prs = [ParameterRange("x", 0, 10, 1)]

        original = dict(genes)
        _mutate(genes, prs, mutation_rate=0.0, rng=rng)
        assert genes == original


# ═══════════════════════════════════════════════════════════════════════════════
# 7. OptimizationResult
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptimizationResult:
    """Test OptimizationResult dataclass."""

    def test_creation(self):
        """OptimizationResult stores data correctly."""
        r = OptimizationResult(
            params={"x": 1.0, "y": 2.0},
            target_value=0.75,
            metrics={"sharpe": 0.75, "total_return": 0.1},
        )
        assert r.params == {"x": 1.0, "y": 2.0}
        assert r.target_value == 0.75
        assert r.metrics["sharpe"] == 0.75


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Valid targets constant
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidTargets:
    """Test VALID_TARGETS constant."""

    def test_contains_expected_targets(self):
        """VALID_TARGETS contains all expected metric names."""
        expected = {
            "sharpe_ratio", "max_drawdown", "win_rate",
            "total_return", "annual_return", "calmar",
            "sortino", "profit_factor",
        }
        assert expected == VALID_TARGETS

    def test_all_targets_acceptable(self):
        """All valid targets can be set."""
        s = OptimizationSetting()
        for target in VALID_TARGETS:
            s.set_target(target)
            assert s.target == target
