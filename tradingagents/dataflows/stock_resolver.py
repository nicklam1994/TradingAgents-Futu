# -*- coding: utf-8 -*-
"""Stock universe resolver — validates tickers against a curated universe of
US stocks, US ETFs, and HK stocks (35k+ entries sourced from DSA index).

Provides:
- ``resolve_ticker(code)`` — check if a ticker exists, return canonical form
- ``to_futu(code)`` / ``to_yfinance(code)`` / ``to_display(code)`` — format converters
- Lazy-loaded in-memory index with O(1) lookup
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


def _load() -> Dict[str, StockEntry]:
    global _INDEX, _INDEX_BY_UPPER, _INDEX_BY_NAME
    if _INDEX is not None:
        return _INDEX

    with _LOCK:
        if _INDEX is not None:
            return _INDEX

        for p in _candidate_paths():
            if p.is_file():
                with p.open("r", encoding="utf-8") as f:
                    items: list[dict] = json.load(f)
                by_code: Dict[str, StockEntry] = {}
                by_upper: Dict[str, StockEntry] = {}
                for item in items:
                    entry = StockEntry(
                        code=item["code"],
                        name=item["name"],
                        market=item["market"],
                        type=item["type"],
                    )
                    by_code[entry["code"]] = entry
                    by_upper[entry["code"].upper()] = entry
                    # Also index by bare ticker without .HK for quick lookup
                    bare = entry["code"].upper().replace(".HK", "")
                    if bare not in by_upper:
                        by_upper[bare] = entry
                    # Index by stock name (Chinese + English)
                    if entry.get("name"):
                        by_upper[entry["name"].upper()] = entry
                _INDEX = by_code
                _INDEX_BY_UPPER = by_upper
                _INDEX_BY_NAME = by_upper
                logger.info("[StockUniverse] Loaded %d entries from %s", len(by_code), p)
                return by_code

        _INDEX = {}
        _INDEX_BY_UPPER = {}
        logger.warning("[StockUniverse] No %s found; resolver disabled", _UNIVERSE_FILENAME)
        return _INDEX


def _get_by_upper() -> Dict[str, StockEntry]:
    _load()
    return _INDEX_BY_UPPER  # type: ignore[return-type]


def _get_by_name() -> Dict[str, StockEntry]:
    """Name→entry index (Chinese + English names, uppercase keys)."""
    _load()
    return _INDEX_BY_NAME  # type: ignore[return-type]


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
    """Convert to Futu prefix format: ``US.AAPL`` / ``HK.00700``."""
    entry = resolve_ticker(code)
    if entry:
        c = entry["code"]
        if entry["market"] == "HK":
            return f"HK.{c.replace('.HK', '')}"
        return f"US.{c}"
    # Fallback heuristic
    s = code.strip().upper()
    if s.endswith(".HK"):
        return f"HK.{s[:-3]}"
    if s.startswith("HK."):
        return s
    if s.startswith("US."):
        return s
    return f"US.{s}"


def to_yfinance(code: str) -> str:
    """Convert to yfinance format: bare ticker for US, CODE.HK for HK."""
    entry = resolve_ticker(code)
    if entry:
        return entry["code"]
    return code.strip().upper()


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
