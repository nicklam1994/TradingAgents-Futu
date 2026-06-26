# -*- coding: utf-8 -*-
"""Real account performance metrics.

Uses Futu `history_deal_list_query` (real account only) to get historical deals,
then computes FIFO-based quantitative metrics (same as sim performance):
- Win rate, max drawdown, Sharpe, Sortino, Calmar
Plus current position snapshot for P&L overview.

Deals are cached to local DB (real_deals table) as backup.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Query range: from 2024-01-01 to now
_QUERY_START = "2024-01-01 00:00:00"


def get_real_performance() -> dict[str, Any]:
    """Compute real account performance from Futu historical deals + position snapshot."""
    # ── 1. Fetch historical deals ───────────────────────────────────────
    all_deals = _fetch_deals_from_futu()

    # Save to local DB as backup
    if all_deals:
        _save_deals_to_db(all_deals)

    # Merge with DB cache (in case Futu returns partial data)
    db_deals = _load_deals_from_db()
    merged = _merge_deals(all_deals, db_deals)

    # ── 2. FIFO matching ────────────────────────────────────────────────
    trade_returns, trade_details = _fifo_match(merged)

    # ── 3. Quant metrics ────────────────────────────────────────────────
    metrics = _compute_metrics(trade_returns, merged)

    # ── 4. Current position snapshot ────────────────────────────────────
    positions = _get_positions()

    total_market_val = sum(p["market_val"] for p in positions)
    total_cost = sum(p["cost_price"] * p["qty"] for p in positions)
    total_pl_val = sum(p["pl_val"] for p in positions)
    total_pl_ratio = (total_pl_val / total_cost * 100) if total_cost > 0 else 0.0

    profitable = [p for p in positions if p["is_profitable"]]
    losing = [p for p in positions if not p["is_profitable"]]
    pos_win_rate = len(profitable) / len(positions) if positions else 0.0

    hk_positions = [p for p in positions if p["market"] == "HK"]
    us_positions = [p for p in positions if p["market"] == "US"]
    best = max(positions, key=lambda p: p["pl_ratio"]) if positions else None
    worst = min(positions, key=lambda p: p["pl_ratio"]) if positions else None

    for p in positions:
        p.pop("market", None)
    if best:
        best = {k: v for k, v in best.items() if k != "market"}
    if worst:
        worst = {k: v for k, v in worst.items() if k != "market"}

    return {
        "ok": True,
        "data": {
            **metrics,
            "total_market_val": round(total_market_val, 2),
            "total_cost": round(total_cost, 2),
            "total_pl_val": round(total_pl_val, 2),
            "total_pl_ratio": round(total_pl_ratio, 4),
            "position_count": len(positions),
            "profitable_count": len(profitable),
            "losing_count": len(losing),
            "position_win_rate": round(pos_win_rate, 4),
            "hk_pl_val": round(sum(p["pl_val"] for p in hk_positions), 2),
            "us_pl_val": round(sum(p["pl_val"] for p in us_positions), 2),
            "hk_count": len(hk_positions),
            "us_count": len(us_positions),
            "best_position": best,
            "worst_position": worst,
            "positions": positions,
            "recent_trades": trade_details[-10:],
        },
    }


# ── Futu API ─────────────────────────────────────────────────────────────────

def _fetch_deals_from_futu() -> list[dict]:
    """Fetch historical deals from Futu API (HK only, since US returns same data)."""
    from futu import RET_OK, TrdEnv, TrdMarket, OpenSecTradeContext, SecurityFirm, SysConfig

    deals: list[dict] = []
    seen_ids: set[str] = set()

    # Only query HK market — Futu returns all deals regardless of market filter
    for market_label, trd_market in [("HK", TrdMarket.HK)]:
        ctx = None
        try:
            SysConfig.enable_proto_encrypt(False)
            ctx = OpenSecTradeContext(
                filter_trdmarket=trd_market,
                host="127.0.0.1",
                port=11111,
                security_firm=SecurityFirm.FUTUSECURITIES,
            )
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ret, data = ctx.history_deal_list_query(
                trd_env=TrdEnv.REAL, start=_QUERY_START, end=now,
            )
            if ret != RET_OK or data is None:
                logger.warning("[RealPerf] %s history_deal failed: %s", market_label, data)
                continue

            for _, row in data.iterrows():
                deal_id = str(row.get("deal_id", ""))
                if deal_id in seen_ids:
                    continue
                seen_ids.add(deal_id)

                side = str(row.get("trd_side", "")).lower()
                code = str(row.get("code", ""))
                price = float(row.get("price", 0) or 0)
                qty = float(row.get("qty", 0) or 0)
                create_time = str(row.get("create_time", ""))
                stock_name = str(row.get("stock_name", ""))

                # Normalize code
                if not code.startswith("HK.") and not code.startswith("US."):
                    if code.isdigit():
                        code = f"HK.{code.zfill(5)}"
                    else:
                        code = f"{code}.US"

                if price <= 0 or qty <= 0:
                    continue

                deals.append({
                    "deal_id": deal_id,
                    "side": side,
                    "code": code,
                    "stock_name": stock_name,
                    "price": price,
                    "qty": qty,
                    "create_time": create_time,
                })
        except Exception as e:
            logger.error("[RealPerf] %s history_deal error: %s", market_label, e)
        finally:
            if ctx:
                ctx.close()

    deals.sort(key=lambda d: d["create_time"])
    logger.info("[RealPerf] Fetched %d deals from Futu", len(deals))
    return deals


# ── Local DB backup ──────────────────────────────────────────────────────────

def _save_deals_to_db(deals: list[dict]) -> int:
    """Save deals to local real_deals table. Returns count of new inserts."""
    from api.database import SessionLocal, RealDealDB

    db = SessionLocal()
    inserted = 0
    try:
        for d in deals:
            existing = db.query(RealDealDB).filter(RealDealDB.deal_id == d["deal_id"]).first()
            if existing:
                continue
            db.add(RealDealDB(
                deal_id=d["deal_id"],
                side=d["side"],
                code=d["code"],
                stock_name=d.get("stock_name", ""),
                price=d["price"],
                qty=d["qty"],
                create_time=d["create_time"],
            ))
            inserted += 1
        db.commit()
        if inserted > 0:
            logger.info("[RealPerf] Saved %d new deals to DB", inserted)
    except Exception as e:
        logger.warning("[RealPerf] DB save error: %s", e)
        db.rollback()
    finally:
        db.close()
    return inserted


def _load_deals_from_db() -> list[dict]:
    """Load all deals from local real_deals table."""
    try:
        from api.database import SessionLocal, RealDealDB
        db = SessionLocal()
        try:
            rows = db.query(RealDealDB).order_by(RealDealDB.create_time).all()
            return [
                {
                    "deal_id": r.deal_id,
                    "side": r.side,
                    "code": r.code,
                    "stock_name": r.stock_name or "",
                    "price": r.price,
                    "qty": r.qty,
                    "create_time": r.create_time,
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception as e:
        logger.warning("[RealPerf] DB load error: %s", e)
        return []


def _merge_deals(futu_deals: list[dict], db_deals: list[dict]) -> list[dict]:
    """Merge Futu deals with DB cache, dedup by deal_id."""
    seen: set[str] = set()
    merged: list[dict] = []
    for d in futu_deals + db_deals:
        if d["deal_id"] not in seen:
            seen.add(d["deal_id"])
            merged.append(d)
    merged.sort(key=lambda d: d["create_time"])
    return merged


# ── FIFO matching ────────────────────────────────────────────────────────────

def _fifo_match(deals: list[dict]) -> tuple[list[float], list[dict]]:
    """FIFO buy/sell matching. Returns (trade_returns, trade_details)."""
    from collections import defaultdict, deque

    buy_queue: dict[str, deque] = defaultdict(deque)
    trade_returns: list[float] = []
    trade_details: list[dict] = []

    for d in deals:
        side = d["side"]
        code = d["code"]
        price = d["price"]
        qty = d["qty"]

        if side == "buy":
            buy_queue[code].append((price, qty))
        elif side == "sell" and buy_queue.get(code):
            remaining = qty
            while remaining > 0 and buy_queue[code]:
                buy_price, buy_qty = buy_queue[code][0]
                matched = min(remaining, buy_qty)
                if buy_price > 0:
                    ret_val = (price - buy_price) / buy_price
                    trade_returns.append(ret_val)
                    trade_details.append({
                        "code": code,
                        "buy_price": buy_price,
                        "sell_price": price,
                        "qty": matched,
                        "return_pct": round(ret_val * 100, 2),
                    })
                remaining -= matched
                if matched >= buy_qty:
                    buy_queue[code].popleft()
                else:
                    buy_queue[code][0] = (buy_price, buy_qty - matched)

    return trade_returns, trade_details


# ── Quant metrics ────────────────────────────────────────────────────────────

def _compute_metrics(trade_returns: list[float], deals: list[dict]) -> dict:
    """Compute quant metrics from trade returns with proper time-based annualization."""
    if not trade_returns:
        return {
            "max_drawdown": 0.0, "sharpe_ratio": 0.0, "sortino_ratio": 0.0,
            "win_rate": 0.0, "calmar_ratio": 0.0, "trade_count": 0,
        }

    # Build equity curve
    base_capital = 100_000.0
    equity_curve: list[float] = [base_capital]
    for r in trade_returns:
        equity_curve.append(equity_curve[-1] * (1.0 + r))

    n = len(trade_returns)
    mdd = 0.0
    if len(equity_curve) >= 2:
        peak = equity_curve[0]
        max_dd = 0.0
        for v in equity_curve:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        mdd = max_dd

    # Time-based annualization: compute actual span in days
    from datetime import datetime as dt
    trade_dates = []
    for d in deals:
        try:
            trade_dates.append(dt.fromisoformat(d["create_time"].split(".")[0]))
        except (ValueError, AttributeError):
            pass
    if len(trade_dates) >= 2:
        span_days = max(1, (max(trade_dates) - min(trade_dates)).days)
    else:
        span_days = n  # fallback: assume 1 trade per day

    years = max(span_days / 365.25, 0.01)

    # Win rate
    wins = sum(1 for r in trade_returns if r > 0)
    win_rate = wins / n if n > 0 else 0.0

    # Sharpe (annualized)
    mean_ret = sum(trade_returns) / n
    var = sum((r - mean_ret) ** 2 for r in trade_returns) / (n - 1) if n >= 2 else 0.0
    std = var ** 0.5
    # Annualize: scale per-trade std by sqrt(trades per year)
    trades_per_year = n / years if years > 0 else n
    sharpe = (mean_ret / std) * (trades_per_year ** 0.5) if std > 0 else 0.0

    # Sortino (annualized)
    downside_sq = [min(0.0, r) ** 2 for r in trade_returns]
    downside_var = sum(downside_sq) / (n - 1) if n >= 2 else 0.0
    downside_std = downside_var ** 0.5
    sortino = (mean_ret / downside_std) * (trades_per_year ** 0.5) if downside_std > 0 else 0.0

    # Calmar: annualized return / max drawdown
    total_return = equity_curve[-1] / equity_curve[0] - 1.0
    annual_return = (1.0 + total_return) ** (1.0 / years) - 1.0
    calmar = (annual_return / mdd) if mdd > 0 else 0.0

    return {
        "max_drawdown": round(mdd, 6),
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "win_rate": round(win_rate, 4),
        "calmar_ratio": round(calmar, 4),
        "trade_count": n,
    }


# ── Position snapshot ────────────────────────────────────────────────────────

def _get_positions() -> list[dict]:
    """Get current real account positions from Futu."""
    from futu import RET_OK, TrdEnv, TrdMarket, OpenSecTradeContext, SecurityFirm, SysConfig

    positions: list[dict] = []
    seen_codes: set[str] = set()

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
                continue

            for _, row in data.iterrows():
                qty = float(row.get("qty", 0) or 0)
                if qty <= 0:
                    continue

                code = str(row.get("code", ""))
                if market_label == "HK":
                    if not code.startswith("HK."):
                        code = f"HK.{code.zfill(5)}"
                elif market_label == "US":
                    if not code.startswith("US.") and "." not in code:
                        code = f"{code}.US"

                if code in seen_codes:
                    continue
                seen_codes.add(code)

                cost_price = float(row.get("cost_price", 0) or 0)
                nominal = float(row.get("nominal_price", 0) or 0)
                last_price = float(row.get("last_price", 0) or 0)
                current_price = nominal if nominal > 0 else last_price
                market_val = float(row.get("market_val", 0) or 0)
                pl_val = float(row.get("unrealized_pl", 0) or 0)
                pl_ratio = float(row.get("pl_ratio", 0) or 0)

                positions.append({
                    "code": code,
                    "name": str(row.get("stock_name", "")),
                    "qty": qty,
                    "cost_price": round(cost_price, 3),
                    "current_price": round(current_price, 3),
                    "market_val": round(market_val, 2),
                    "pl_val": round(pl_val, 2),
                    "pl_ratio": round(pl_ratio, 4),
                    "is_profitable": pl_val > 0,
                    "market": market_label,
                })
        except Exception as e:
            logger.warning("[RealPerf] position query %s error: %s", market_label, e)
        finally:
            if ctx:
                ctx.close()

    # Fetch lot_size from quote snapshot
    if positions:
        try:
            from futu import OpenQuoteContext
            all_futu_codes = [p["code"] for p in positions]
            qctx = OpenQuoteContext(host="127.0.0.1", port=11111, security_firm=SecurityFirm.FUTUSECURITIES)
            try:
                ret2, snap = qctx.get_market_snapshot(all_futu_codes)
                if ret2 == RET_OK and snap is not None and not snap.empty:
                    lot_map = {}
                    for _, sr in snap.iterrows():
                        lot_map[str(sr.get("code", ""))] = int(sr.get("lot_size", 0) or 0)
                    for p in positions:
                        p["lot_size"] = lot_map.get(p["code"], 0)
            finally:
                qctx.close()
        except Exception:
            for p in positions:
                p.setdefault("lot_size", 0)

    return positions
