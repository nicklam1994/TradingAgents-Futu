"""Shadow Account — delta PnL attribution.

Computes the difference between user's realized trades and shadow rules.
Pure arithmetic — no LLM, no simulation rebuild.

Attribution categories:
    - missed_signals_pnl: PnL from rules the user didn't follow
    - noise_trades_pnl: PnL from trades that violated all rules
    - early_exit_pnl: PnL lost by closing winning trades too early
    - late_exit_pnl: PnL lost by holding losing trades too long
    - overtrading_pnl: PnL from excessive trading frequency
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from tradingagents.shadow.models import AttributionBreakdown, ShadowProfile
from tradingagents.shadow.extractor import pair_trades_fifo


def compute_attribution(
    profile: ShadowProfile,
    trades_df: pd.DataFrame,
) -> AttributionBreakdown:
    """Compute delta PnL attribution between real trades and shadow rules.

    Args:
        profile: ShadowProfile with extracted rules.
        trades_df: DataFrame with trade history (symbol, datetime, side, price, qty).

    Returns:
        AttributionBreakdown with signed PnL for each category.
    """
    if trades_df.empty:
        return AttributionBreakdown(
            missed_signals_pnl=0.0,
            noise_trades_pnl=0.0,
            early_exit_pnl=0.0,
            late_exit_pnl=0.0,
            overtrading_pnl=0.0,
        )

    # Pair trades
    roundtrips = pair_trades_fifo(trades_df)
    if not roundtrips:
        return AttributionBreakdown(
            missed_signals_pnl=0.0,
            noise_trades_pnl=0.0,
            early_exit_pnl=0.0,
            late_exit_pnl=0.0,
            overtrading_pnl=0.0,
        )

    # Categorize roundtrips
    rule_matching = []
    rule_violating = []

    for rt in roundtrips:
        if _matches_any_rule(rt, profile.rules):
            rule_matching.append(rt)
        else:
            rule_violating.append(rt)

    # 1. Noise trades: trades that violated all rules
    noise_trades_pnl = -sum(rt["pnl"] for rt in rule_violating if rt["pnl"] < 0)

    # 2. Early exit: winning trades closed before minimum holding period
    early_exit_pnl = 0.0
    for rt in rule_matching:
        if rt["pnl"] > 0:
            min_hold = _get_min_hold(rt, profile.rules)
            if rt["hold_days"] < min_hold:
                # Estimate lost PnL (simplified: assume could have held to min)
                early_exit_pnl += rt["pnl"] * 0.5  # 50% of profit lost

    # 3. Late exit: losing trades held beyond maximum holding period
    late_exit_pnl = 0.0
    for rt in rule_matching:
        if rt["pnl"] < 0:
            max_hold = _get_max_hold(rt, profile.rules)
            if rt["hold_days"] > max_hold:
                # Estimate excess loss (simplified: 30% of loss could be avoided)
                late_exit_pnl += rt["pnl"] * 0.3  # negative PnL

    # 4. Overtrading: too many trades in a short period
    overtrading_pnl = 0.0
    if len(roundtrips) > 20:  # More than 20 trades is potentially overtrading
        # Estimate noise from excessive frequency
        excess_ratio = len(rule_violating) / max(len(roundtrips), 1)
        if excess_ratio > 0.3:  # More than 30% rule violations
            overtrading_pnl = -sum(abs(rt["pnl"]) for rt in rule_violating) * 0.2

    # 5. Missed signals: residual (shadow PnL - real PnL - other categories)
    real_total_pnl = sum(rt["pnl"] for rt in roundtrips)
    shadow_total_pnl = sum(rt["pnl"] for rt in rule_matching)
    missed_signals_pnl = shadow_total_pnl - real_total_pnl - noise_trades_pnl - early_exit_pnl - late_exit_pnl - overtrading_pnl

    return AttributionBreakdown(
        missed_signals_pnl=round(missed_signals_pnl, 2),
        noise_trades_pnl=round(noise_trades_pnl, 2),
        early_exit_pnl=round(early_exit_pnl, 2),
        late_exit_pnl=round(late_exit_pnl, 2),
        overtrading_pnl=round(overtrading_pnl, 2),
    )


def _matches_any_rule(rt: dict[str, Any], rules: tuple) -> bool:
    """Check if a roundtrip matches any shadow rule."""
    for rule in rules:
        if _matches_rule(rt, rule):
            return True
    return False


def _matches_rule(rt: dict[str, Any], rule) -> bool:
    """Check if a roundtrip matches a specific rule."""
    # Check holding days
    hold_lo, hold_hi = rule.holding_days_range
    if not (hold_lo <= rt["hold_days"] <= hold_hi):
        return False

    # Check market if specified
    market = rule.entry_condition.get("market")
    if market and market != "other":
        rt_market = "HK" if rt["symbol"].startswith("HK.") else "US"
        if market != rt_market:
            return False

    return True


def _get_min_hold(rt: dict[str, Any], rules: tuple) -> float:
    """Get minimum holding days from matching rules."""
    for rule in rules:
        if _matches_rule(rt, rule):
            return rule.holding_days_range[0]
    return 1.0


def _get_max_hold(rt: dict[str, Any], rules: tuple) -> float:
    """Get maximum holding days from matching rules."""
    for rule in rules:
        if _matches_rule(rt, rule):
            return rule.holding_days_range[1]
    return 30.0
