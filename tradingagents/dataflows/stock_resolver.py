# -*- coding: utf-8 -*-
"""Stock universe resolver — validates tickers against the DB-first stock index.

Provides:
- ``resolve_ticker(code)`` — check if a ticker exists, return canonical form
- ``to_futu(code)`` / ``to_yfinance(code)`` / ``to_display(code)`` — format converters
- Lazy-loaded in-memory index from DB with JSON fallback
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, TypedDict

logger = logging.getLogger(__name__)

# ── Data types ────────────────────────────────────────────────────────────────

class StockEntry(TypedDict):
    code: str       # TAF canonical: "AAPL", "00700.HK"
    name: str       # Chinese display name
    market: str     # "US" | "ETF" | "HK"
    type: str       # "stock" | "etf"


# ── Singleton cache ───────────────────────────────────────────────────────────

_INDEX: Optional[Dict[str, StockEntry]] = None
_INDEX_BY_UPPER: Optional[Dict[str, StockEntry]] = None
_INDEX_BY_NAME: Optional[Dict[str, StockEntry]] = None
_LOCK = Lock()

_UNIVERSE_FILENAME = "stock_universe.json"


def _candidate_paths() -> tuple[Path, ...]:
    here = Path(__file__).resolve().parent
    return (here / _UNIVERSE_FILENAME,)


def _load_from_db() -> Dict[str, StockEntry]:
    """Load all stocks from DB into StockEntry dict."""
    try:
        from api.database import SessionLocal, StockDB
        db = SessionLocal()
        try:
            rows = db.query(StockDB.code, StockDB.name).all()
            result: Dict[str, StockEntry] = {}
            for code_val, name_val in rows:
                # Determine market from code suffix
                code_str = str(code_val)
                market = "HK" if code_str.endswith(".HK") else "US"
                entry = StockEntry(
                    code=code_str,
                    name=str(name_val or ""),
                    market=market,
                    type="stock",
                )
                result[code_str] = entry
            return result
        finally:
            db.close()
    except Exception as e:
        logger.warning("[StockUniverse] DB load failed: %s", e)
        return {}


def _load_from_json() -> Dict[str, StockEntry]:
    """Load stocks from stock_universe.json (fallback/backup)."""
    for p in _candidate_paths():
        if p.is_file():
            with p.open("r", encoding="utf-8") as f:
                items: list[dict] = json.load(f)
            result: Dict[str, StockEntry] = {}
            for item in items:
                entry = StockEntry(
                    code=item["code"],
                    name=item["name"],
                    market=item["market"],
                    type=item["type"],
                )
                result[entry["code"]] = entry
            return result
    return {}


def _load() -> Dict[str, StockEntry]:
    """Load stock index: DB-first, JSON fallback for missing entries."""
    global _INDEX, _INDEX_BY_UPPER, _INDEX_BY_NAME
    if _INDEX is not None:
        return _INDEX

    with _LOCK:
        if _INDEX is not None:
            return _INDEX

        # 1. Load from DB (primary source)
        db_entries = _load_from_db()

        # 2. Load from JSON (fallback for entries not in DB)
        json_entries = _load_from_json()

        # 3. Merge: DB takes priority
        merged: Dict[str, StockEntry] = {}
        merged.update(json_entries)  # JSON as base
        merged.update(db_entries)    # DB overrides (correct names from Futu)

        _INDEX = merged
        _INDEX_BY_UPPER = {}
        _INDEX_BY_NAME = {}

        for entry in merged.values():
            _INDEX_BY_UPPER[entry["code"].upper()] = entry
            # Also index by bare ticker without .HK or .US for quick lookup
            bare = entry["code"].upper()
            for suffix in (".HK", ".US"):
                if bare.endswith(suffix):
                    bare = bare[: -len(suffix)]
                    break
            if bare and bare not in _INDEX_BY_UPPER:
                _INDEX_BY_UPPER[bare] = entry
            # Index by stock name (Chinese + English)
            if entry.get("name"):
                _INDEX_BY_NAME[entry["name"].upper()] = entry

        logger.info(
            "[StockUniverse] Loaded %d entries (DB=%d, JSON=%d, merged=%d)",
            len(merged), len(db_entries), len(json_entries), len(merged),
        )
        return merged


def _get_by_upper() -> Dict[str, StockEntry]:
    _load()
    return _INDEX_BY_UPPER  # type: ignore[return-type]


def _get_by_name() -> Dict[str, StockEntry]:
    """Name→entry index (Chinese + English names, uppercase keys)."""
    _load()
    return _INDEX_BY_NAME  # type: ignore[return-type]


# ── Sync DB → JSON (backup) ──────────────────────────────────────────────────

def sync_to_json() -> int:
    """Write current DB stocks back to stock_universe.json as backup.

    Returns number of entries written.
    """
    db_entries = _load_from_db()
    if not db_entries:
        logger.warning("[StockUniverse] No DB entries to sync")
        return 0

    path = _candidate_paths()[0]
    items = [
        {"code": e["code"], "name": e["name"], "market": e["market"], "type": e["type"]}
        for e in db_entries.values()
    ]
    # Sort by market then code for readability
    items.sort(key=lambda x: (x["market"], x["code"]))
    with path.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    logger.info("[StockUniverse] Synced %d entries to %s", len(items), path)
    return len(items)


# ── Public API ────────────────────────────────────────────────────────────────

def resolve_ticker(code: str) -> Optional[StockEntry]:
    """Look up *code* in the stock universe.  Returns the entry or ``None``.

    Accepts any common format: ``AAPL``, ``00700.HK``, ``HK.00700``, ``SPY``.
    """
    if not code:
        return None
    s = code.strip().upper()

    # Strip common prefixes/suffixes to get bare lookup key
    bare = s
    for prefix in ("US.", "HK."):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
    for suffix in (".US", ".HK", ".SH", ".SZ", ".SS"):
        if bare.endswith(suffix):
            bare = bare[: -len(suffix)]

    idx = _get_by_upper()

    # Try exact match first
    entry = idx.get(s) or idx.get(bare)
    if entry:
        return entry

    # Try zero-padded HK code
    if bare.isdigit() and len(bare) <= 5:
        padded = bare.zfill(5)
        entry = idx.get(f"{padded}.HK") or idx.get(padded)
        if entry:
            return entry

    return None


def search_by_name(name: str) -> Optional[StockEntry]:
    """Search stock by Chinese or English name (exact or substring match).

    Returns the best match or None.
    Examples: search_by_name("商汤") → 00020.HK, search_by_name("腾讯") → 00700.HK
    """
    results = search_by_name_multi(name)
    return results[0] if results else None


def search_by_name_multi(name: str, limit: int = 5) -> List[StockEntry]:
    """Search stock by name, returning multiple candidates for disambiguation.

    Returns up to *limit* unique entries (by code) sorted by match quality.
    Examples:
        search_by_name_multi("腾讯") → [腾讯控股(00700.HK), 腾讯音乐(TME)]
        search_by_name_multi("百度") → [百度(BIDU), 百度集团-SW(09888.HK)]
    """
    if not name:
        return []
    idx = _get_by_name()
    s = name.strip()

    # Exact match (case-insensitive) — still check substring for more candidates
    entry = idx.get(s.upper())
    if entry:
        # Exact match found, but also check for substring matches (e.g. "百度" → BIDU + 百度集团)
        seen_codes: set = {entry["code"]}
        candidates: List[tuple] = [(3, len(entry.get("name", "")), entry)]  # score 3 for exact
    else:
        seen_codes = set()
        candidates = []

    # Strip -W/-S/-B suffixes common in HK
    if not entry:
        for suffix in ("-W", "-S", "-B", "-SW", "-R"):
            if s.endswith(suffix):
                bare_name = s[: -len(suffix)]
                entry = idx.get(bare_name.upper())
                if entry:
                    return [entry]

    # Substring match: collect all candidates, deduplicate by code
    s_lower = s.lower()
    for key, e in idx.items():
        code = e["code"]
        if code in seen_codes:
            continue
        name_val = e.get("name", "").lower()
        if not name_val or len(name_val) < 2:
            continue
        if s_lower not in name_val and name_val not in s_lower:
            continue
        seen_codes.add(code)
        score = 2 if name_val.startswith(s_lower) else 1
        candidates.append((score, len(name_val), e))

    # Sort: highest score first, then shortest name
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return [c[2] for c in candidates[:limit]]


def resolve_input(raw: str) -> str:
    """Resolve ANY user input (Chinese name, ticker, code) to canonical symbol.

    Priority: resolve_ticker(code) → search_by_name(chinese) → upper(raw)
    Examples: "商汤" → "00020.HK", "AAPL" → "AAPL", "00020.HK" → "00020.HK"
    """
    if not raw:
        return raw
    s = raw.strip()
    # Try code-based lookup first (AAPL, 00020.HK, HK.00020)
    entry = resolve_ticker(s)
    if entry:
        return entry["code"]
    # Try Chinese name lookup
    entry = search_by_name(s)
    if entry:
        return entry["code"]
    # Fallback: uppercase
    return s.upper()


def is_known_ticker(code: str) -> bool:
    """Quick boolean check."""
    return resolve_ticker(code) is not None


# ── Format converters ─────────────────────────────────────────────────────────

def to_canonical(code: str) -> str:
    """Normalize to TAF canonical: bare ticker for US, CODE.HK for HK."""
    entry = resolve_ticker(code)
    if entry:
        return entry["code"]
    # Fallback: apply same heuristic as _normalize_symbol
    return code.strip().upper()


def to_futu(code: str) -> str:
    """Convert to Futu format: ``HK.00700``, ``US.AAPL``."""
    entry = resolve_ticker(code)
    canonical = entry["code"] if entry else code.strip().upper()

    if canonical.endswith(".HK"):
        return f"HK.{canonical[:-3]}"
    if canonical.endswith(".US"):
        return f"US.{canonical[:-3]}"
    if canonical.startswith("HK."):
        return canonical
    if canonical.startswith("US."):
        return canonical
    # Bare ticker → assume US
    return f"US.{canonical}"


def to_yfinance(code: str) -> str:
    """Convert to yfinance format: ``AAPL``, ``0700.HK``."""
    entry = resolve_ticker(code)
    canonical = entry["code"] if entry else code.strip().upper()

    if canonical.endswith(".HK"):
        return canonical.replace(".HK", ".HK")  # already correct
    return canonical


def to_display(code: str) -> str:
    """Return human-readable display: "AAPL (APPLE)" or just the code."""
    entry = resolve_ticker(code)
    if entry and entry["name"]:
        return f"{entry['code']} ({entry['name']})"
    return code.strip().upper()


def to_futu_trade(code: str) -> tuple[str, str]:
    """Convert to ``(futu_full_code, market_str)`` for sim trading.

    Returns ``(\"US.AAPL\", \"US\")`` or ``(\"HK.00700\", \"HK\")``.
    """
    futu_code = to_futu(code)
    if futu_code.startswith("HK."):
        return futu_code, "HK"
    return futu_code, "US"


def universe_size() -> int:
    """Return the number of entries in the loaded universe."""
    return len(_load())


# ── Pure format utilities (from code_format.py) ──────────────────────────────

def to_pure(code: str) -> str:
    """Extract pure code without market suffix/prefix.

    Examples:
        "00700.HK" -> "00700"
        "HK.00700" -> "00700"
        "AAPL.US"  -> "AAPL"
        "US.AAPL"  -> "AAPL"
        "AAPL"     -> "AAPL"
    """
    if not code:
        return code
    s = code.strip().upper()
    for prefix in ("HK.", "US.", "SH.", "SZ."):
        if s.startswith(prefix):
            return s[len(prefix):]
    if "." in s:
        parts = s.split(".")
        if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
            return parts[0]
    return s


def detect_market(code: str) -> str | None:
    """Detect market from any code format.

    Returns: "HK", "US", "SH", "SZ", or None if unknown.

    Examples:
        "00700.HK" -> "HK"
        "HK.00700" -> "HK"
        "AAPL.US"  -> "US"
        "AAPL"     -> "US"  (inferred)
        "00700"    -> "HK"  (inferred)
    """
    if not code:
        return None
    s = code.strip().upper()

    # Futu format: MARKET.CODE
    for prefix in ("HK.", "US.", "SH.", "SZ."):
        if s.startswith(prefix):
            return prefix[:2]

    # Canonical format: CODE.MARKET
    if "." in s:
        parts = s.split(".")
        if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
            return parts[1]

    # Pure code — infer
    if s.isdigit():
        return "HK"
    if s.isalpha():
        return "US"
    return None


def is_valid_code(code: str) -> bool:
    """Check if code is a valid stock code in any format."""
    if not code:
        return False
    s = code.strip().upper()
    # Futu or canonical format
    if s.startswith(("HK.", "US.", "SH.", "SZ.")):
        return len(s) > 3
    if "." in s:
        parts = s.split(".")
        if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
            return len(parts[0]) > 0
    # Pure code
    return s.isalnum() and len(s) >= 4


# ── Cache management ──────────────────────────────────────────────────────────

def reload() -> None:
    """Force reload from DB + JSON on next access."""
    global _INDEX, _INDEX_BY_UPPER, _INDEX_BY_NAME
    with _LOCK:
        _INDEX = None
        _INDEX_BY_UPPER = None
        _INDEX_BY_NAME = None
    _load()
