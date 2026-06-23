"""Shadow Account — strategy extraction from profitable roundtrips.

Adapted from Vibe-Trading agent/src/shadow_account/extractor.py.
Simplified for TAF's Futu trade history (HK/US stocks/ETFs).

Pipeline:
    trades_df → FIFO pair → filter (pnl > 0) → feature engineer
    → KMeans cluster (k auto 2-5) → per-cluster stats
    → structured entry_condition dict
    → template-based natural-language translation

Design constraints:
    * No external price-data calls in v1. All features are derivable from
      the journal itself (holding_days, pnl_pct, entry hour/weekday, market).
    * Must survive tiny samples: <5 profitable roundtrips → explicit error.
      <2 clusters → degrade to a single-cluster heuristic rule.
    * Rules are immutable ShadowRule objects — scanner's only input.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd

from tradingagents.shadow.models import ShadowProfile, ShadowRule

logger = logging.getLogger(__name__)

MIN_PROFITABLE_ROUNDTRIPS = 5
DEFAULT_MAX_RULES = 5
DEFAULT_MIN_SUPPORT = 3
_NUMERIC_FEATURES = ("holding_days", "pnl_pct", "entry_hour", "entry_weekday")
_CATEGORICAL_FEATURES = ("market",)

# Market labels for display
_MARKET_LABELS = {
    "HK": "港股",
    "US": "美股",
    "other": "其他",
}

RULE_TEXT_MAX = 80


# ── Public API ───────────────────────────────────────────────────────────────

def extract_shadow_profile(
    trades_df: pd.DataFrame,
    *,
    min_support: int = DEFAULT_MIN_SUPPORT,
    max_rules: int = DEFAULT_MAX_RULES,
) -> ShadowProfile:
    """Extract a ShadowProfile from a trade history DataFrame.

    Args:
        trades_df: DataFrame with columns:
            - symbol: str (e.g., "HK.00700", "US.AAPL")
            - datetime: datetime (trade time)
            - side: str ("buy" or "sell")
            - price: float
            - qty: float
            - market: str ("HK" or "US")
        min_support: Minimum profitable roundtrips backing any single rule.
        max_rules: Cap on the number of rules returned.

    Returns:
        ShadowProfile (not yet persisted — caller decides whether to save).

    Raises:
        ValueError: Fewer than MIN_PROFITABLE_ROUNDTRIPS profitable roundtrips.
    """
    if trades_df.empty:
        raise ValueError("No trade records provided.")

    # Ensure required columns
    required = {"symbol", "datetime", "side", "price", "qty"}
    missing = required - set(trades_df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # Add market column if not present
    if "market" not in trades_df.columns:
        trades_df = trades_df.copy()
        trades_df["market"] = trades_df["symbol"].apply(
            lambda s: "HK" if str(s).startswith("HK.") else "US"
        )

    # FIFO pair trades
    roundtrips = pair_trades_fifo(trades_df)
    total = len(roundtrips)
    if total == 0:
        raise ValueError("No complete buy→sell roundtrips found.")

    profitable = [rt for rt in roundtrips if rt["pnl"] > 0]
    if len(profitable) < MIN_PROFITABLE_ROUNDTRIPS:
        raise ValueError(
            f"Insufficient profitable roundtrips: {len(profitable)} "
            f"(need ≥{MIN_PROFITABLE_ROUNDTRIPS})."
        )

    # Feature engineering
    features_df = _compute_features(profitable, trades_df)

    # Extract rules
    rules = _extract_rules(
        features_df,
        min_support=min_support,
        max_rules=max_rules,
    )

    # Build profile
    source_market = _dominant(trades_df["market"])
    preferred_markets = tuple(trades_df["market"].value_counts().index.tolist())
    hold = features_df["holding_days"].dropna()
    typical_holding = (
        round(float(hold.median()), 2) if len(hold) else 0.0,
        round(float(hold.quantile(0.75)), 2) if len(hold) else 0.0,
    )
    date_range = (
        str(trades_df["datetime"].min()),
        str(trades_df["datetime"].max()),
    )
    profile_text = _render_profile_text(
        total_profitable=len(profitable),
        total_all=total,
        typical_holding=typical_holding,
        source_market=source_market,
        preferred_markets=preferred_markets,
    )

    # Generate shadow_id from content hash
    content_hash = hashlib.sha1(
        str(trades_df.to_dict()).encode()
    ).hexdigest()[:8]

    return ShadowProfile(
        shadow_id=f"shadow_{content_hash}",
        created_at=datetime.utcnow().isoformat(),
        journal_hash=content_hash,
        source_market=source_market,
        profitable_roundtrips=len(profitable),
        total_roundtrips=total,
        date_range=date_range,
        profile_text=profile_text,
        rules=tuple(rules),
        preferred_markets=preferred_markets,
        typical_holding_days=typical_holding,
    )


# ── FIFO Trade Pairing ───────────────────────────────────────────────────────

def pair_trades_fifo(trades_df: pd.DataFrame) -> list[dict[str, Any]]:
    """Pair buy/sell trades using FIFO method.

    Args:
        trades_df: DataFrame with symbol, datetime, side, price, qty columns.

    Returns:
        List of roundtrip dicts with keys:
            symbol, buy_dt, sell_dt, buy_price, sell_price, qty, pnl, pnl_pct, hold_days
    """
    trades_df = trades_df.sort_values("datetime")
    roundtrips = []

    for symbol, group in trades_df.groupby("symbol"):
        # Separate buys and sells
        buys = group[group["side"].str.lower() == "buy"].copy()
        sells = group[group["side"].str.lower() == "sell"].copy()

        # FIFO matching
        buy_queue = []
        for _, buy_row in buys.iterrows():
            buy_queue.append({
                "dt": buy_row["datetime"],
                "price": float(buy_row["price"]),
                "qty": float(buy_row["qty"]),
                "remaining": float(buy_row["qty"]),
            })

        for _, sell_row in sells.iterrows():
            sell_qty = float(sell_row["qty"])
            sell_price = float(sell_row["price"])
            sell_dt = sell_row["datetime"]

            while sell_qty > 0 and buy_queue:
                buy = buy_queue[0]
                matched_qty = min(sell_qty, buy["remaining"])

                if matched_qty > 0:
                    pnl = (sell_price - buy["price"]) * matched_qty
                    pnl_pct = (sell_price / buy["price"] - 1.0) if buy["price"] > 0 else 0.0
                    hold_days = max(0, (sell_dt - buy["dt"]).total_seconds() / 86400)

                    roundtrips.append({
                        "symbol": symbol,
                        "buy_dt": buy["dt"],
                        "sell_dt": sell_dt,
                        "buy_price": buy["price"],
                        "sell_price": sell_price,
                        "qty": matched_qty,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "hold_days": hold_days,
                    })

                    buy["remaining"] -= matched_qty
                    sell_qty -= matched_qty

                if buy["remaining"] <= 0:
                    buy_queue.pop(0)

    return roundtrips


# ── Feature Engineering ──────────────────────────────────────────────────────

def _compute_features(
    roundtrips: list[dict[str, Any]],
    trades_df: pd.DataFrame,
) -> pd.DataFrame:
    """Compute a features row per profitable roundtrip.

    Columns: symbol, market, holding_days, pnl, pnl_pct, entry_hour, entry_weekday
    """
    market_by_symbol = (
        trades_df.drop_duplicates("symbol").set_index("symbol")["market"].to_dict()
    )
    rows: list[dict[str, Any]] = []
    for rt in roundtrips:
        buy_dt = pd.Timestamp(rt["buy_dt"])
        rows.append({
            "symbol": rt["symbol"],
            "market": market_by_symbol.get(rt["symbol"], "other"),
            "holding_days": float(rt["hold_days"]),
            "pnl": float(rt["pnl"]),
            "pnl_pct": float(rt["pnl_pct"]),
            "entry_hour": int(buy_dt.hour),
            "entry_weekday": int(buy_dt.weekday()),
            "buy_dt": buy_dt,
            "sell_dt": pd.Timestamp(rt["sell_dt"]),
        })
    return pd.DataFrame(rows)


# ── Rule Extraction ──────────────────────────────────────────────────────────

def _extract_rules(
    features_df: pd.DataFrame,
    *,
    min_support: int,
    max_rules: int,
) -> list[ShadowRule]:
    """Cluster profitable roundtrips, derive one rule per dense cluster."""
    if len(features_df) < min_support:
        return [_heuristic_single_rule(features_df, min_support)]

    try:
        cluster_labels = _auto_cluster(features_df, max_k=min(max_rules, 5))
    except Exception as exc:
        logger.warning("Clustering failed, using heuristic: %s", exc)
        return [_heuristic_single_rule(features_df, min_support)]

    rules: list[ShadowRule] = []
    total_profitable = len(features_df)
    used_keys: set[tuple] = set()

    for cluster_id in sorted(set(cluster_labels)):
        cluster_mask = cluster_labels == cluster_id
        cluster_df = features_df[cluster_mask]
        if len(cluster_df) < min_support:
            continue
        rule = _cluster_to_rule(
            cluster_df=cluster_df,
            rule_index=len(rules) + 1,
            total_profitable=total_profitable,
        )
        # Deduplicate near-identical rules
        key = (rule.entry_condition.get("market"), rule.holding_days_range)
        if key in used_keys:
            continue
        used_keys.add(key)
        rules.append(rule)
        if len(rules) >= max_rules:
            break

    if not rules:
        rules = [_heuristic_single_rule(features_df, min_support)]
    return rules


def _auto_cluster(features_df: pd.DataFrame, *, max_k: int) -> np.ndarray:
    """Pick a cluster count via simple silhouette heuristic."""
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    numeric = features_df[list(_NUMERIC_FEATURES)].astype(float).to_numpy()
    if len(numeric) <= 2 or max_k < 2:
        return np.zeros(len(numeric), dtype=int)
    scaled = StandardScaler().fit_transform(numeric)

    best_k, best_score = 2, -1.0
    try:
        from sklearn.metrics import silhouette_score
        for k in range(2, min(max_k, len(numeric) - 1) + 1):
            labels = KMeans(n_clusters=k, n_init=5, random_state=42).fit_predict(scaled)
            if len(set(labels)) < 2:
                continue
            score = silhouette_score(scaled, labels)
            if score > best_score:
                best_k, best_score = k, score
    except Exception as exc:
        logger.debug("silhouette selection failed, fallback k=2: %s", exc)

    return KMeans(n_clusters=best_k, n_init=5, random_state=42).fit_predict(scaled)


def _cluster_to_rule(
    *,
    cluster_df: pd.DataFrame,
    rule_index: int,
    total_profitable: int,
) -> ShadowRule:
    """Summarize a cluster as one ShadowRule."""
    market = _dominant(cluster_df["market"])
    hold_days = cluster_df["holding_days"]
    hold_lo = max(1, int(round(float(hold_days.quantile(0.10)))))
    hold_hi = max(hold_lo, int(round(float(hold_days.quantile(0.90)))))
    hours = cluster_df["entry_hour"]
    hour_lo = int(round(float(hours.quantile(0.10))))
    hour_hi = int(round(float(hours.quantile(0.90))))

    entry_condition: dict[str, Any] = {
        "market": market,
        "entry_hour": {"min": hour_lo, "max": hour_hi},
    }
    exit_condition: dict[str, Any] = {
        "holding_days": {"min": hold_lo, "max": hold_hi},
    }

    samples = tuple(
        f"{row.symbol}@{pd.Timestamp(row.buy_dt).date().isoformat()}"
        for row in cluster_df.head(3).itertuples(index=False)
    )
    support = int(len(cluster_df))
    coverage = round(support / max(total_profitable, 1), 3)

    human = _translate_rule(
        entry_condition=entry_condition,
        exit_condition=exit_condition,
        holding_range=(hold_lo, hold_hi),
    )

    return ShadowRule(
        rule_id=f"R{rule_index}",
        human_text=human,
        entry_condition=entry_condition,
        exit_condition=exit_condition,
        holding_days_range=(hold_lo, hold_hi),
        support_count=support,
        coverage_rate=coverage,
        sample_trades=samples,
    )


def _heuristic_single_rule(
    features_df: pd.DataFrame,
    min_support: int,
) -> ShadowRule:
    """Degenerate fallback when clustering yields nothing usable."""
    return _cluster_to_rule(
        cluster_df=features_df,
        rule_index=1,
        total_profitable=max(len(features_df), min_support),
    )


# ── Natural-language Translation ─────────────────────────────────────────────

def _translate_rule(
    *,
    entry_condition: dict[str, Any],
    exit_condition: dict[str, Any],
    holding_range: tuple[int, int],
) -> str:
    """Turn a structured rule dict into a concise Chinese sentence."""
    market_label = _MARKET_LABELS.get(entry_condition.get("market", "other"), "其他")
    hour_range = entry_condition.get("entry_hour", {})
    hour_text = ""
    if hour_range:
        lo, hi = hour_range.get("min"), hour_range.get("max")
        hour_text = f" {lo}:00" if lo == hi else f" {lo}:00-{hi}:00"
    hold_lo, hold_hi = holding_range
    hold_text = f"持有{hold_lo}-{hold_hi}天" if hold_lo != hold_hi else f"持有{hold_lo}天"
    return f"{market_label}{hour_text}买入，{hold_text}"[:RULE_TEXT_MAX]


# ── Utilities ────────────────────────────────────────────────────────────────

def _dominant(series: pd.Series) -> str:
    """Most frequent value in a series."""
    if series.empty:
        return "other"
    return str(series.value_counts().idxmax())


def _render_profile_text(
    *,
    total_profitable: int,
    total_all: int,
    typical_holding: tuple[float, float],
    source_market: str,
    preferred_markets: tuple[str, ...],
) -> str:
    """Build a one-paragraph Chinese portrait."""
    median, p75 = typical_holding
    markets_label = "、".join(_MARKET_LABELS.get(m, m) for m in preferred_markets[:3])
    source_label = _MARKET_LABELS.get(source_market, source_market)
    return (
        f"在{total_all}笔交易中，{total_profitable}笔盈利。"
        f"主要交易{source_label}（也活跃于{markets_label}）。"
        f"中位持仓{median:.1f}天，75%仓位在{p75:.1f}天内平仓。"
    )
