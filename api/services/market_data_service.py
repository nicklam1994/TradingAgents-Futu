"""Market data refresh service — syncs plates, stock-plates, and stocks from Futu to local DB.

Functions:
    refresh_plates        – Futu get_plate_list → plates table
    refresh_stock_plates  – Futu get_plate_stock → stock_plates table
    refresh_stocks        – Futu get_stock_basicinfo → stocks table
    import_stock_universe – stock_universe.json → stocks table (insert-if-missing)
    refresh_all           – runs all of the above in order
    get_plates_from_db    – read plates from DB
    resolve_plate_code_from_db – fuzzy plate name → code
    get_stocks_in_plate   – JOIN stocks + stock_plates
    search_stocks_from_db – search stocks by code prefix or name substring
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from api.database import get_db_ctx, PlateDB, StockPlateDB, StockDB

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Futu connection helpers
# ---------------------------------------------------------------------------

def _opend_host() -> str:
    return os.getenv("FUTU_OPEND_HOST", "127.0.0.1")

def _opend_port() -> int:
    return int(os.getenv("FUTU_OPEND_PORT", "11111"))


def _futu_to_canonical(futu_code: str) -> str:
    """Convert Futu format (HK.00001, US.AAPL) to canonical (00001.HK, AAPL)."""
    parts = futu_code.split(".", 1)
    if len(parts) == 2:
        return f"{parts[1]}.{parts[0]}"
    return futu_code


def _canonical_to_futu(canonical: str) -> str:
    """Convert canonical (00001.HK, AAPL) to Futu format (HK.00001)."""
    if "." in canonical:
        code_part, market = canonical.rsplit(".", 1)
        return f"{market}.{code_part}"
    return canonical


def _ensure_tables() -> None:
    """Create market-data tables if they don't exist yet."""
    from api.database import engine, Base
    Base.metadata.create_all(bind=engine, tables=[
        PlateDB.__table__,
        StockPlateDB.__table__,
        StockDB.__table__,
    ])


# ---------------------------------------------------------------------------
# 1. refresh_plates
# ---------------------------------------------------------------------------

def refresh_plates(market: str = "HK") -> int:
    """Fetch industry plates from Futu and upsert into the *plates* table.

    Returns the number of rows upserted.
    """
    from futu import OpenQuoteContext, Plate, RET_OK

    _ensure_tables()
    logger.info("[market_data] refresh_plates market=%s", market)
    ctx = OpenQuoteContext(host=_opend_host(), port=_opend_port())
    try:
        ret, data = ctx.get_plate_list(market=market, plate_class=Plate.INDUSTRY)
        if ret != RET_OK or data is None:
            logger.warning("[market_data] get_plate_list failed: ret=%s data=%s", ret, data)
            return 0

        count = 0
        with get_db_ctx() as db:
            for _, row in data.iterrows():
                plate_code = str(row.get("code", row.get("plate_code", "")))
                plate_name = str(row.get("plate_name", ""))
                plate_id = str(row.get("plate_id", ""))
                if not plate_code:
                    continue
                stmt = sqlite_insert(PlateDB).values(
                    code=plate_code,
                    name=plate_name,
                    market=market,
                    plate_id=plate_id,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["code"],
                    set_={"name": plate_name, "market": market, "plate_id": plate_id},
                )
                db.execute(stmt)
                count += 1
            db.commit()

        logger.info("[market_data] refresh_plates done: %d plates upserted", count)
        return count
    except Exception:
        logger.exception("[market_data] refresh_plates error")
        return 0
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 2. refresh_stock_plates
# ---------------------------------------------------------------------------

def refresh_stock_plates(market: str = "HK") -> int:
    """For every plate in DB, fetch constituent stocks from Futu and upsert
    into the *stock_plates* table (ON CONFLICT DO NOTHING).

    Returns the total number of stock-plate mappings inserted.
    """
    from futu import OpenQuoteContext, RET_OK

    _ensure_tables()
    logger.info("[market_data] refresh_stock_plates market=%s", market)

    # Collect plate codes from DB for this market
    with get_db_ctx() as db:
        plates = db.execute(
            select(PlateDB.code).where(PlateDB.market == market)
        ).scalars().all()

    if not plates:
        logger.warning("[market_data] No plates found in DB for market=%s. Run refresh_plates first.", market)
        return 0

    logger.info("[market_data] refresh_stock_plates: %d plates to process", len(plates))
    ctx = OpenQuoteContext(host=_opend_host(), port=_opend_port())
    total_inserted = 0
    import time
    try:
        for i, plate_code in enumerate(plates):
            try:
                # Futu rate limit: get_plate_stock max 10 per 30s
                if i > 0:
                    time.sleep(3)
                ret, data = ctx.get_plate_stock(plate_code)
                if ret != RET_OK or data is None:
                    logger.warning("[market_data] get_plate_stock(%s) failed: ret=%s", plate_code, ret)
                    continue

                rows_to_insert = []
                for _, row in data.iterrows():
                    futu_code = str(row.get("code", ""))
                    canonical = _futu_to_canonical(futu_code)
                    if not canonical:
                        continue
                    rows_to_insert.append({
                        "stock_code": canonical,
                        "plate_code": plate_code,
                    })

                if rows_to_insert:
                    with get_db_ctx() as db:
                        stmt = sqlite_insert(StockPlateDB).values(rows_to_insert)
                        stmt = stmt.on_conflict_do_nothing()
                        db.execute(stmt)
                        db.commit()
                        total_inserted += len(rows_to_insert)

            except Exception:
                logger.exception("[market_data] refresh_stock_plates error for plate %s", plate_code)
                continue

        logger.info("[market_data] refresh_stock_plates done: %d stock-plate rows processed", total_inserted)
        return total_inserted
    except Exception:
        logger.exception("[market_data] refresh_stock_plates unexpected error")
        return total_inserted
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 3. refresh_stocks
# ---------------------------------------------------------------------------

def refresh_stocks(market: str = "HK") -> int:
    """Fetch basic stock info from Futu for all stocks in the given market and
    upsert into the *stocks* table with lot_size.

    Returns the number of rows upserted.
    """
    from futu import OpenQuoteContext, SecurityType, RET_OK

    _ensure_tables()
    logger.info("[market_data] refresh_stocks market=%s", market)
    ctx = OpenQuoteContext(host=_opend_host(), port=_opend_port())
    try:
        ret, data = ctx.get_stock_basicinfo(market, SecurityType.STOCK)
        if ret != RET_OK or data is None:
            logger.warning("[market_data] get_stock_basicinfo failed: ret=%s data=%s", ret, data)
            return 0

        count = 0
        with get_db_ctx() as db:
            for _, row in data.iterrows():
                futu_code = str(row.get("code", ""))
                canonical = _futu_to_canonical(futu_code)
                name = str(row.get("name", ""))
                lot_size = int(row["lot_size"]) if "lot_size" in row and row["lot_size"] is not None else None

                stmt = sqlite_insert(StockDB).values(
                    code=canonical,
                    name=name,
                    market=market,
                    type="stock",
                    lot_size=lot_size,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["code"],
                    set_={"name": name, "market": market, "type": "stock", "lot_size": lot_size},
                )
                db.execute(stmt)
                count += 1

            db.commit()

        logger.info("[market_data] refresh_stocks done: %d stocks upserted", count)
        return count
    except Exception:
        logger.exception("[market_data] refresh_stocks error")
        return 0
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 4. import_stock_universe
# ---------------------------------------------------------------------------

_STOCK_UNIVERSE_PATH = Path(__file__).resolve().parents[2] / "tradingagents" / "dataflows" / "stock_universe.json"


def import_stock_universe() -> int:
    """Read stock_universe.json and insert missing stocks into the *stocks* table.

    Only rows whose *code* is not already present are inserted (no overwrite).
    Returns the number of newly inserted rows.
    """
    _ensure_tables()
    logger.info("[market_data] import_stock_universe from %s", _STOCK_UNIVERSE_PATH)
    if not _STOCK_UNIVERSE_PATH.exists():
        logger.warning("[market_data] stock_universe.json not found at %s", _STOCK_UNIVERSE_PATH)
        return 0

    with open(_STOCK_UNIVERSE_PATH, "r", encoding="utf-8") as f:
        universe = json.load(f)

    inserted = 0
    with get_db_ctx() as db:
        # Fetch existing codes once for efficient lookup
        existing = {row[0] for row in db.execute(select(StockDB.code)).all()}

        batch: list[dict[str, Any]] = []
        for entry in universe:
            code = entry.get("code", "")
            if not code or code in existing:
                continue
            batch.append({
                "code": code,
                "name": entry.get("name", ""),
                "market": entry.get("market", ""),
                "type": entry.get("type", "stock"),
                "lot_size": None,
            })

            # Flush in chunks of 500
            if len(batch) >= 500:
                stmt = sqlite_insert(StockDB).values(batch)
                stmt = stmt.on_conflict_do_nothing()
                db.execute(stmt)
                inserted += len(batch)
                batch.clear()

        # Flush remaining
        if batch:
            stmt = sqlite_insert(StockDB).values(batch)
            stmt = stmt.on_conflict_do_nothing()
            db.execute(stmt)
            inserted += len(batch)

        db.commit()

    logger.info("[market_data] import_stock_universe done: %d new rows inserted", inserted)
    return inserted


# ---------------------------------------------------------------------------
# 5. refresh_all
# ---------------------------------------------------------------------------

def refresh_all(market: str = "HK") -> dict[str, int]:
    """Run all refresh steps in order and return counts.

    Order: plates → stock_plates → stocks → import_stock_universe
    """
    logger.info("[market_data] refresh_all market=%s — starting", market)
    plates_count = refresh_plates(market)
    stock_plates_count = refresh_stock_plates(market)
    stocks_count = refresh_stocks(market)
    universe_count = import_stock_universe()
    result = {
        "plates": plates_count,
        "stock_plates": stock_plates_count,
        "stocks": stocks_count,
        "universe_imported": universe_count,
    }
    logger.info("[market_data] refresh_all done: %s", result)
    return result


# ---------------------------------------------------------------------------
# 6. get_plates_from_db
# ---------------------------------------------------------------------------

def get_plates_from_db(market: str) -> list[dict[str, Any]]:
    """Return all plates for *market* from the local DB."""
    with get_db_ctx() as db:
        rows = db.execute(
            select(PlateDB).where(PlateDB.market == market)
        ).scalars().all()
        return [
            {"code": r.code, "name": r.name, "market": r.market, "plate_id": r.plate_id}
            for r in rows
        ]


# ---------------------------------------------------------------------------
# 7. resolve_plate_code_from_db
# ---------------------------------------------------------------------------

def resolve_plate_code_from_db(category: str, market: str = "HK") -> str | None:
    """Search the plates table by substring match on *name* (case-insensitive).

    Returns the plate code of the first match, or None.
    """
    if not category:
        return None
    with get_db_ctx() as db:
        row = db.execute(
            select(PlateDB.code).where(
                PlateDB.market == market,
                PlateDB.name.contains(category),
            )
        ).scalars().first()
        return row


# ---------------------------------------------------------------------------
# 8. get_stocks_in_plate
# ---------------------------------------------------------------------------

def get_stocks_in_plate(plate_code: str) -> list[dict[str, Any]]:
    """Return stocks belonging to *plate_code* by joining stocks + stock_plates."""
    if not plate_code:
        return []
    with get_db_ctx() as db:
        rows = db.execute(
            select(StockDB.code, StockDB.name, StockDB.market, StockDB.type, StockDB.lot_size)
            .join(StockPlateDB, StockPlateDB.stock_code == StockDB.code)
            .where(StockPlateDB.plate_code == plate_code)
        ).all()
        return [
            {"code": r.code, "name": r.name, "market": r.market, "type": r.type, "lot_size": r.lot_size}
            for r in rows
        ]


# ---------------------------------------------------------------------------
# 9. search_stocks_from_db
# ---------------------------------------------------------------------------

def search_stocks_from_db(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search the stocks table by code prefix or name substring.

    *query* matches against stock code (prefix) or name (contains, case-insensitive).
    """
    if not query or not query.strip():
        return []
    with get_db_ctx() as db:
        rows = db.execute(
            select(StockDB).where(
                or_(
                    StockDB.code.startswith(query),
                    StockDB.name.contains(query),
                )
            ).limit(limit)
        ).scalars().all()
        return [
            {"code": r.code, "name": r.name, "market": r.market, "type": r.type, "lot_size": r.lot_size}
            for r in rows
        ]
