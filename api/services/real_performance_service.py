# -*- coding: utf-8 -*-
"""Real account performance metrics.

Since Futu does not provide historical deal data for real accounts,
we compute metrics from the current position snapshot:
- Per-position P&L (cost vs current price)
- Win rate (% of profitable positions)
- Total unrealized P&L
- Position distribution by market
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_real_performance() -> dict[str, Any]:
    """Compute real account performance from Futu position snapshots."""
    from futu import RET_OK, TrdEnv, TrdMarket, OpenSecTradeContext, SecurityFirm, SysConfig

    positions: list[dict] = []

    for market_label, trd_market in [("HK", TrdMarket.HK), ("US", TrdMarket.US)]:
        ctx = None
        try:
            SysConfig.enable_proto_encrypt(False)
            ctx = OpenSecTradeContext(
                filter_trdmarket=trd_market,
                host="127.0.0.1",
                port=11111,
                security_firm=SecurityFirm.FUTUSECURITIES,
            )
            ret, data = ctx.position_list_query(trd_env=TrdEnv.REAL)
            if ret != RET_OK or data is None:
                logger.warning("[RealPerf] %s position query failed: %s", market_label, data)
                continue

            for _, row in data.iterrows():
                qty = float(row.get("qty", 0) or 0)
                if qty <= 0:
                    continue

                code = str(row.get("code", ""))
                # Normalize code — Futu returns bare code like "07500", "AAPL"
                if market_label == "HK":
                    if not code.startswith("HK."):
                        code = f"HK.{code.zfill(5)}"
                elif market_label == "US":
                    if not code.startswith("US.") and "." not in code:
                        code = f"{code}.US"

                cost_price = float(row.get("cost_price", 0) or 0)
                nominal = float(row.get("nominal_price", 0) or 0)
                last_price = float(row.get("last_price", 0) or 0)
                current_price = nominal if nominal > 0 else last_price
                market_val = float(row.get("market_val", 0) or 0)
                pl_val = float(row.get("unrealized_pl", 0) or 0)
                pl_ratio_raw = float(row.get("pl_ratio", 0) or 0)
                # Futu pl_ratio: 0.0069 = 0.69%, already percentage-ish
                # For HK stocks, pl_ratio is already in percentage (1.27 = 1.27%)
                # For US stocks, same convention
                pl_ratio = round(pl_ratio_raw, 4)

                positions.append({
                    "code": code,
                    "name": str(row.get("stock_name", "")),
                    "qty": qty,
                    "cost_price": cost_price,
                    "current_price": current_price,
                    "market_val": market_val,
                    "pl_val": pl_val,
                    "pl_ratio": pl_ratio,
                    "is_profitable": pl_val > 0,
                    "market": market_label,
                })
        except Exception as e:
            logger.error("[RealPerf] %s query error: %s", market_label, e)
        finally:
            if ctx:
                ctx.close()

    # Deduplicate by code (Futu may return same positions for HK/US queries)
    seen_codes: set[str] = set()
    unique_positions: list[dict] = []
    for p in positions:
        if p["code"] not in seen_codes:
            seen_codes.add(p["code"])
            unique_positions.append(p)
    positions = unique_positions
    total_market_val = sum(p["market_val"] for p in positions)
    total_cost = sum(p["cost_price"] * p["qty"] for p in positions)
    total_pl_val = sum(p["pl_val"] for p in positions)
    total_pl_ratio = (total_pl_val / total_cost * 100) if total_cost > 0 else 0.0

    profitable = [p for p in positions if p["is_profitable"]]
    losing = [p for p in positions if not p["is_profitable"]]
    win_rate = len(profitable) / len(positions) if positions else 0.0

    hk_positions = [p for p in positions if p["market"] == "HK"]
    us_positions = [p for p in positions if p["market"] == "US"]

    best = max(positions, key=lambda p: p["pl_ratio"]) if positions else None
    worst = min(positions, key=lambda p: p["pl_ratio"]) if positions else None

    # Remove internal 'market' field from output
    for p in positions:
        p.pop("market", None)
    if best:
        best = {k: v for k, v in best.items() if k != "market"}
    if worst:
        worst = {k: v for k, v in worst.items() if k != "market"}

    return {
        "ok": True,
        "data": {
            "total_market_val": round(total_market_val, 2),
            "total_cost": round(total_cost, 2),
            "total_pl_val": round(total_pl_val, 2),
            "total_pl_ratio": round(total_pl_ratio, 4),
            "position_count": len(positions),
            "profitable_count": len(profitable),
            "losing_count": len(losing),
            "win_rate": round(win_rate, 4),
            "hk_pl_val": round(sum(p["pl_val"] for p in hk_positions), 2),
            "us_pl_val": round(sum(p["pl_val"] for p in us_positions), 2),
            "hk_count": len(hk_positions),
            "us_count": len(us_positions),
            "best_position": best,
            "worst_position": worst,
            "positions": positions,
        },
    }
