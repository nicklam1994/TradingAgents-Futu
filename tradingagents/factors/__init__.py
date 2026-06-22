"""Alpha Zoo — quantitative factor library for TAF.

Provides 50+ pre-built cross-sectional alpha factors ported from Vibe-Trading,
organized by theme (momentum, reversal, volatility, volume, microstructure).

Usage:
    from tradingagents.factors.registry import get_default_registry

    registry = get_default_registry()
    ic = registry.compute_ic("gtja191_alpha_003", panel, forward_returns)
    results = registry.compute_batch(registry.list(theme="momentum"), panel)
"""

from tradingagents.factors.base import (
    Alpha,
    AlphaCompute,
    Market,
    decay_linear,
    delta,
    rank,
    safe_div,
    scale,
    signed_power,
    ts_argmax,
    ts_argmin,
    ts_corr,
    ts_cov,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
    vwap,
)
from tradingagents.factors.registry import (
    AlphaMeta,
    Registry,
    RegistryError,
    SkipAlpha,
    get_default_registry,
    reset_default_registry,
)

__all__ = [
    "Alpha",
    "AlphaCompute",
    "AlphaMeta",
    "Market",
    "Registry",
    "RegistryError",
    "SkipAlpha",
    "decay_linear",
    "delta",
    "get_default_registry",
    "rank",
    "reset_default_registry",
    "safe_div",
    "scale",
    "signed_power",
    "ts_argmax",
    "ts_argmin",
    "ts_corr",
    "ts_cov",
    "ts_max",
    "ts_mean",
    "ts_min",
    "ts_rank",
    "ts_std",
    "vwap",
]
