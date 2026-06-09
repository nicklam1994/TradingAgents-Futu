"""Relative strength service — compare stock vs market index and sector.

Uses Futu API to calculate:
1. Stock's daily return
2. Market index's daily return (HSI for HK, SPX for US)
3. Stock's sector's daily return
4. Calculate relative strength = stock return - benchmark return
"""

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

def _opend_host() -> str:
    return os.getenv("FUTU_OPEND_HOST", "127.0.0.1")

def _opend_port() -> int:
    return int(os.getenv("FUTU_OPEND_PORT", "11111"))

@dataclass
class RelativeStrengthResult:
    """Relative strength comparison result."""
    stock_symbol: str
    stock_name: str
    stock_change_pct: float | None
    
    # Market index comparison
    market_index: str | None  # e.g., "HSI", "SPX"
    market_change_pct: float | None
    vs_market: float | None  # stock_change - market_change
    
    # Sector comparison
    sector_name: str | None
    sector_change_pct: float | None
    vs_sector: float | None  # stock_change - sector_change
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "stock_symbol": self.stock_symbol,
            "stock_name": self.stock_name,
            "stock_change_pct": self.stock_change_pct,
            "market_index": self.market_index,
            "market_change_pct": self.market_change_pct,
            "vs_market": self.vs_market,
            "sector_name": self.sector_name,
            "sector_change_pct": self.sector_change_pct,
            "vs_sector": self.vs_sector,
        }

def get_relative_strength(symbol: str) -> RelativeStrengthResult | None:
    """Calculate relative strength for a stock vs market index and sector.
    
    Args:
        symbol: Canonical stock code (e.g., "00700.HK", "AAPL")
    
    Returns:
        RelativeStrengthResult or None if calculation fails
    """
    try:
        from futu import OpenQuoteContext, SysConfig, Plate, Market
        
        SysConfig.enable_proto_encrypt(False)
        ctx = OpenQuoteContext(host=_opend_host(), port=_opend_port())
        
        try:
            # 1. Get stock quote
            futu_code = _to_futu_code(symbol)
            ret, stock_data = ctx.get_market_snapshot([futu_code])
            if ret != 0 or stock_data is None or stock_data.empty:
                logger.warning(f"[relative-strength] Failed to get stock snapshot: {ret}")
                return None
            
            stock_row = stock_data.iloc[0]
            stock_name = stock_row.get("name", symbol)
            stock_change_pct = _safe_float(stock_row.get("price_spread"))
            
            # 2. Get market index
            market = "HK" if symbol.endswith(".HK") else "US"
            index_code = "HK.800000" if market == "HK" else "US.SPX"  # HSI or S&P 500
            index_name = "恆生指數" if market == "HK" else "S&P 500"
            
            ret, index_data = ctx.get_market_snapshot([index_code])
            market_change_pct = None
            if ret == 0 and index_data is not None and not index_data.empty:
                market_change_pct = _safe_float(index_data.iloc[0].get("price_spread"))
            
            # 3. Get stock's sector
            sector_name = None
            sector_change_pct = None
            
            ret, plate_data = ctx.get_owner_plate(futu_code)
            if ret == 0 and plate_data is not None and not plate_data.empty:
                # Get the first industry plate
                industry_plates = plate_data[plate_data["plate_type"] == "INDUSTRY"]
                if not industry_plates.empty:
                    sector_name = industry_plates.iloc[0].get("plate_name")
                    plate_code = industry_plates.iloc[0].get("plate_code")
                    
                    if plate_code:
                        # Get sector snapshot
                        ret, sector_data = ctx.get_market_snapshot([plate_code])
                        if ret == 0 and sector_data is not None and not sector_data.empty:
                            sector_change_pct = _safe_float(sector_data.iloc[0].get("price_spread"))
            
            # 4. Calculate relative strength
            vs_market = None
            if stock_change_pct is not None and market_change_pct is not None:
                vs_market = round(stock_change_pct - market_change_pct, 2)
            
            vs_sector = None
            if stock_change_pct is not None and sector_change_pct is not None:
                vs_sector = round(stock_change_pct - sector_change_pct, 2)
            
            return RelativeStrengthResult(
                stock_symbol=symbol,
                stock_name=stock_name,
                stock_change_pct=stock_change_pct,
                market_index=index_name,
                market_change_pct=market_change_pct,
                vs_market=vs_market,
                sector_name=sector_name,
                sector_change_pct=sector_change_pct,
                vs_sector=vs_sector,
            )
            
        finally:
            ctx.close()
            
    except Exception as exc:
        logger.error(f"[relative-strength] Error calculating for {symbol}: {exc}")
        return None

def _to_futu_code(symbol: str) -> str:
    """Convert canonical symbol to Futu format.
    
    "00700.HK" -> "HK.00700"
    "AAPL" -> "US.AAPL"
    """
    if symbol.endswith(".HK"):
        return f"HK.{symbol[:-3]}"
    elif symbol.endswith(".US"):
        return f"US.{symbol[:-3]}"
    elif symbol.endswith(".SH"):
        return f"SH.{symbol[:-3]}"
    elif symbol.endswith(".SZ"):
        return f"SZ.{symbol[:-3]}"
    else:
        # Assume US stock
        return f"US.{symbol}"

def _safe_float(val: Any) -> float | None:
    """Safely convert to float, return None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
