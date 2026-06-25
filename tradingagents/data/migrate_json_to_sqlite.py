# -*- coding: utf-8 -*-
"""Migration script: stock_universe.json → SQLite (stock_resolver tables).

Migrates the stock universe from the JSON file into the existing
stock_resolver tables (plates/stocks/stock_plates) in api.database.

This provides backward compatibility while consolidating data into SQLite.

Usage:
    python -m tradingagents.data.migrate_json_to_sqlite
    # or
    from tradingagents.data.migrate_json_to_sqlite import migrate_stock_universe
    count = migrate_stock_universe()

Phase: P1-5 数据统一到 SQLite
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Path to stock_universe.json
_UNIVERSE_PATH = Path(__file__).resolve().parent / "stock_universe.json"


def _load_json_universe(path: Optional[Path] = None) -> List[Dict]:
    """Load stock entries from stock_universe.json.

    Args:
        path: Path to JSON file. Defaults to stock_universe.json in same directory.

    Returns:
        List of dicts with keys: code, name, market, type
    """
    p = path or _UNIVERSE_PATH
    if not p.is_file():
        logger.warning("[Migrate] JSON file not found: %s", p)
        return []

    with p.open("r", encoding="utf-8") as f:
        items = json.load(f)

    logger.info("[Migrate] Loaded %d entries from %s", len(items), p)
    return items


def migrate_stock_universe(
    json_path: Optional[Path] = None,
    dry_run: bool = False,
) -> int:
    """Migrate stock_universe.json entries into the SQLite stocks table.

    Uses api.database.StockDB (stock_resolver table) as the target.
    Uses ON CONFLICT DO UPDATE to handle existing entries.

    Args:
        json_path: Path to JSON file. Defaults to stock_universe.json.
        dry_run: If True, only count entries without writing.

    Returns:
        Number of entries migrated (upserted).
    """
    items = _load_json_universe(json_path)
    if not items:
        return 0

    if dry_run:
        logger.info("[Migrate] Dry run: would migrate %d entries", len(items))
        return len(items)

    try:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from api.database import SessionLocal, StockDB, engine, Base

        # Ensure table exists
        Base.metadata.create_all(bind=engine, tables=[StockDB.__table__])

        count = 0
        db = SessionLocal()
        try:
            for item in items:
                code = item.get("code", "")
                name = item.get("name", "")
                market = item.get("market", "")
                stock_type = item.get("type", "stock")

                if not code:
                    continue

                # Upsert with ON CONFLICT DO UPDATE
                stmt = sqlite_insert(StockDB).values(
                    code=code,
                    name=name,
                    market=market,
                    type=stock_type,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["code"],
                    set_={
                        "name": name,
                        "market": market,
                        "type": stock_type,
                    },
                )
                db.execute(stmt)
                count += 1

            db.commit()
            logger.info("[Migrate] Successfully migrated %d entries to SQLite", count)
        except Exception as e:
            db.rollback()
            logger.error("[Migrate] Migration failed: %s", e)
            raise
        finally:
            db.close()

        return count

    except ImportError as e:
        logger.error("[Migrate] Import error (api.database not available): %s", e)
        return 0


def verify_migration(json_path: Optional[Path] = None) -> Dict:
    """Verify migration by comparing JSON and DB counts.

    Returns:
        Dict with verification results.
    """
    items = _load_json_universe(json_path)
    json_count = len(items)

    try:
        from api.database import SessionLocal, StockDB

        db = SessionLocal()
        try:
            db_count = db.query(StockDB).count()

            # Check specific entries
            sample_codes = [item["code"] for item in items[:5]]
            found_in_db = []
            for code in sample_codes:
                row = db.query(StockDB).filter(StockDB.code == code).first()
                if row:
                    found_in_db.append(code)

            return {
                "json_count": json_count,
                "db_count": db_count,
                "match": json_count <= db_count,  # DB may have more entries from Futu refresh
                "sample_verified": len(found_in_db) == len(sample_codes),
                "sample_codes": sample_codes,
                "found_in_db": found_in_db,
            }
        finally:
            db.close()

    except ImportError:
        return {
            "json_count": json_count,
            "db_count": 0,
            "match": False,
            "error": "api.database not available",
        }
    except Exception as e:
        return {
            "json_count": json_count,
            "db_count": 0,
            "match": False,
            "error": str(e),
        }


def sync_json_from_db() -> int:
    """Sync current DB stocks back to stock_universe.json as backup.

    This is the reverse of migration - useful for backup/export.

    Returns:
        Number of entries written to JSON.
    """
    try:
        from api.database import SessionLocal, StockDB

        db = SessionLocal()
        try:
            rows = db.query(StockDB).all()
            items = [
                {
                    "code": row.code,
                    "name": row.name,
                    "market": row.market,
                    "type": row.type,
                }
                for row in rows
            ]
        finally:
            db.close()

        if not items:
            logger.warning("[Migrate] No entries in DB to sync")
            return 0

        # Sort by market then code
        items.sort(key=lambda x: (x["market"], x["code"]))

        path = _UNIVERSE_PATH
        with path.open("w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)

        logger.info("[Migrate] Synced %d entries to %s", len(items), path)
        return len(items)

    except ImportError as e:
        logger.error("[Migrate] Import error: %s", e)
        return 0


# ── CLI Entry Point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "--verify":
        result = verify_migration()
        print(f"JSON entries: {result['json_count']}")
        print(f"DB entries:   {result['db_count']}")
        print(f"Match:        {result['match']}")
        if result.get("sample_verified"):
            print("Sample verification: PASSED")
        else:
            print(f"Sample verification: FAILED (found {result.get('found_in_db', [])})")
    elif len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        count = migrate_stock_universe(dry_run=True)
        print(f"Would migrate {count} entries")
    elif len(sys.argv) > 1 and sys.argv[1] == "--sync-back":
        count = sync_json_from_db()
        print(f"Synced {count} entries back to JSON")
    else:
        count = migrate_stock_universe()
        print(f"Migrated {count} entries to SQLite")
