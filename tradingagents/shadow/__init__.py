"""
tradingagents.shadow — Shadow Account: 交易行为诊断与反事实归因

Extracts implicit trading rules from profitable roundtrips, scans for
similar signals today, and computes delta PnL attribution.

Phase 13.2: Shadow Account (adapted from Vibe-Trading)

Usage:
    from tradingagents.shadow import extract_shadow_profile, scan_today_signals, compute_attribution

    # Extract rules from trade history
    profile = extract_shadow_profile(trades_df)

    # Scan for matching signals today
    signals = scan_today_signals(profile)

    # Generate signal engine code from rules
    from tradingagents.shadow.codegen import render_signal_engine, write_run_dir
    source = render_signal_engine(profile)

    # Run shadow backtest
    from tradingagents.shadow.backtester import run_shadow_backtest
    result = run_shadow_backtest(profile, window_start="2024-01-01", window_end="2024-12-31")
"""
from tradingagents.shadow.models import (
    ShadowRule,
    ShadowProfile,
    AttributionBreakdown,
    ShadowBacktestResult,
)
from tradingagents.shadow.extractor import extract_shadow_profile, pair_trades_fifo
from tradingagents.shadow.scanner import scan_today_signals
from tradingagents.shadow.attribution import compute_attribution
from tradingagents.shadow.codegen import (
    render_signal_engine,
    validate_generated,
    build_config,
    write_run_dir,
)
from tradingagents.shadow.backtester import (
    run_shadow_backtest,
    select_multi_market_codes,
    flatten_codes,
    load_cached_result,
    runs_dir,
)

__all__ = [
    # Models
    "ShadowRule",
    "ShadowProfile",
    "AttributionBreakdown",
    "ShadowBacktestResult",
    # Extraction
    "extract_shadow_profile",
    "pair_trades_fifo",
    # Scanning
    "scan_today_signals",
    # Attribution
    "compute_attribution",
    # Code generation
    "render_signal_engine",
    "validate_generated",
    "build_config",
    "write_run_dir",
    # Backtesting
    "run_shadow_backtest",
    "select_multi_market_codes",
    "flatten_codes",
    "load_cached_result",
    "runs_dir",
]
