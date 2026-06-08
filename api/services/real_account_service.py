"""Real Account Service — Futu OpenD real trading account operations.

Provides account info for real (non-simulated) trading accounts.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ── Configuration helpers ────────────────────────────────────────────────────

def _opend_host() -> str:
    host = os.getenv("FUTU_OPEND_HOST")
    if host:
        return host
    try:
        from dotenv import dotenv_values
        env_vals = dotenv_values(".env")
        host = env_vals.get("FUTU_OPEND_HOST")
        if host:
            return host
    except Exception:
        pass
    return "127.0.0.1"


def _opend_port() -> int:
    port = os.getenv("FUTU_OPEND_PORT")
    if port:
        return int(port)
    try:
        from dotenv import dotenv_values
        env_vals = dotenv_values(".env")
        port = env_vals.get("FUTU_OPEND_PORT")
        if port:
            return int(port)
    except Exception:
        pass
    return 11111


def _get_rsa_path() -> Optional[str]:
    rsa_path = os.getenv("FUTU_RSA_KEY_PATH", "config/rsa_key.txt")
    if not os.path.isabs(rsa_path):
        rsa_path = os.path.join(os.path.dirname(__file__), "..", "..", rsa_path)
    rsa_path = os.path.abspath(rsa_path)
    if not os.path.exists(rsa_path):
        return None
    return rsa_path


def _need_encrypt() -> bool:
    host = _opend_host()
    if host in ("127.0.0.1", "localhost"):
        return False
    return _get_rsa_path() is not None


def _safe_float(val, default=0.0) -> float:
    if val is None or val == "N/A" or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class RealAccountInfo:
    """Real account financial information."""
    market: str = "HK"
    total_assets: float = 0.0
    cash_balance: float = 0.0
    frozen_cash: float = 0.0
    market_val: float = 0.0
    currency: str = "HKD"
    available_cash: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


# ── Core service methods ────────────────────────────────────────────────────

def get_real_account(trd_market: str = "HK") -> RealAccountInfo:
    """Query real trading account financial information.

    Args:
        trd_market: "HK" or "US". Must match the market to see correct account.

    Returns:
        RealAccountInfo with all financial fields populated.
    """
    from futu import RET_OK, TrdEnv, TrdMarket, OpenSecTradeContext, SecurityFirm, SysConfig

    market = TrdMarket.US if trd_market.upper() == "US" else TrdMarket.HK
    encrypt = _need_encrypt()
    if encrypt:
        rsa_path = _get_rsa_path()
        SysConfig.enable_proto_encrypt(is_encrypt=True)
        SysConfig.set_init_rsa_file(rsa_path)
    ctx = OpenSecTradeContext(
        host=_opend_host(),
        port=_opend_port(),
        filter_trdmarket=market,
        security_firm=SecurityFirm.FUTUSECURITIES,
        is_encrypt=True if encrypt else None,
    )
    try:
        ret, data = ctx.accinfo_query(trd_env=TrdEnv.REAL)
        if ret != RET_OK:
            raise RuntimeError(f"Futu accinfo_query failed: {data}")

        if data is None or data.empty:
            raise RuntimeError("No account data returned from FutuOpenD")

        row = data.iloc[0]
        currency_raw = str(row.get("currency", "N/A"))
        currency = currency_raw if currency_raw not in ("N/A", "", "None") else ("USD" if trd_market.upper() == "US" else "HKD")
        return RealAccountInfo(
            market=trd_market.upper(),
            total_assets=_safe_float(row.get("total_assets")),
            cash_balance=_safe_float(row.get("cash")),
            frozen_cash=_safe_float(row.get("frozen_cash")),
            market_val=_safe_float(row.get("market_val")),
            currency=currency,
            available_cash=_safe_float(row.get("poweravl", row.get("cash"))),
            unrealized_pnl=_safe_float(row.get("unrealized_pl")),
            realized_pnl=_safe_float(row.get("realized_pl")),
        )
    finally:
        ctx.close()


def get_all_real_accounts() -> list[RealAccountInfo]:
    """Query both HK and US real accounts."""
    accounts = []
    for market in ["HK", "US"]:
        try:
            acc = get_real_account(market)
            # Fallback: if unrealized/realized are 0, calculate from positions
            if acc.unrealized_pnl == 0 and acc.realized_pnl == 0:
                try:
                    from futu import OpenSecTradeContext, TrdEnv, SecurityFirm, TrdMarket
                    ctx = OpenSecTradeContext(
                        host=_opend_host(),
                        port=_opend_port(),
                        filter_trdmarket=TrdMarket.US if market == "US" else TrdMarket.HK,
                        security_firm=SecurityFirm.FUTUSECURITIES,
                    )
                    ret, pos_data = ctx.position_list_query(trd_env=TrdEnv.REAL)
                    ctx.close()
                    if ret == 0 and pos_data is not None:
                        acc.unrealized_pnl = sum(float(r.get("unrealized_pl", 0) or 0) for _, r in pos_data.iterrows())
                        acc.realized_pnl = sum(float(r.get("realized_pl", 0) or 0) for _, r in pos_data.iterrows())
                except Exception:
                    pass
            accounts.append(acc)
        except Exception as e:
            logger.warning(f"Failed to get {market} real account: {e}")
    return accounts


def account_to_dict(info: RealAccountInfo) -> dict:
    """Serialize RealAccountInfo to dict for API response."""
    return {
        "market": info.market,
        "total_assets": info.total_assets,
        "cash_balance": info.cash_balance,
        "frozen_cash": info.frozen_cash,
        "market_val": info.market_val,
        "currency": info.currency,
        "available_cash": info.available_cash,
        "unrealized_pnl": info.unrealized_pnl,
        "realized_pnl": info.realized_pnl,
    }
