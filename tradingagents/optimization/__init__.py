"""tradingagents.optimization — Parameter optimisation for backtests.

Provides brute-force grid search and genetic algorithm optimisation
for strategy parameters, with multiprocessing support.

Modules:
  - optimizer: OptimizationSetting, run_bf_optimization, run_ga_optimization
"""

from tradingagents.optimization.optimizer import (
    OptimizationSetting,
    OptimizationResult,
    ParameterRange,
    run_bf_optimization,
    run_ga_optimization,
    VALID_TARGETS,
)

__all__ = [
    "OptimizationSetting",
    "OptimizationResult",
    "ParameterRange",
    "run_bf_optimization",
    "run_ga_optimization",
    "VALID_TARGETS",
]
