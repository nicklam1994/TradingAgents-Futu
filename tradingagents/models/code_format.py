"""
Stock Code Format Converter — Single source of truth for all code conversions.

Formats:
  - canonical: "AAPL.US", "00700.HK", "300750.SZ" (DB storage, API exchange)
  - futu:      "US.AAPL", "HK.00700"              (Futu OpenD API)
  - display:   "AAPL.US", "00700.HK"              (UI display, same as canonical)
  - pure:      "AAPL", "00700"                     (user input, no market suffix)

Usage:
    from tradingagents.models.code_format import to_canonical, to_futu, to_pure, detect_market
"""

from __future__ import annotations

# ── Canonical (CODE.MARKET) ──────────────────────────────────────────────────

def to_canonical(code: str) -> str:
    """Convert any format to canonical (CODE.MARKET).
    
    Examples:
        "HK.00700" -> "00700.HK"
        "US.AAPL"  -> "AAPL.US"
        "AAPL.US"  -> "AAPL.US"  (already canonical)
        "AAPL"     -> "AAPL"     (pure, no market info)
        "00700"    -> "00700"    (pure, no market info)
    """
    if not code:
        return code
    
    code = code.strip().upper()
    
    # Already canonical: CODE.MARKET (but not MARKET.CODE)
    if _is_canonical(code):
        return code
    
    # Futu format: MARKET.CODE
    if code.startswith("HK."):
        return code[3:] + ".HK"
    if code.startswith("US."):
        return code[3:] + ".US"
    if code.startswith("SH."):
        return code[3:] + ".SH"
    if code.startswith("SZ."):
        return code[3:] + ".SZ"
    
    # Pure code (no market info)
    return code


def _is_canonical(code: str) -> bool:
    """Check if code is already in canonical format (CODE.MARKET)."""
    parts = code.split(".")
    if len(parts) != 2:
        return False
    code_part, market = parts
    # Market must be 2 letters, code must not be empty
    return len(market) == 2 and market.isalpha() and len(code_part) > 0


# ── Futu (MARKET.CODE) ───────────────────────────────────────────────────────

def to_futu(code: str) -> str:
    """Convert any format to Futu format (MARKET.CODE).
    
    Examples:
        "00700.HK" -> "HK.00700"
        "AAPL.US"  -> "US.AAPL"
        "HK.00700" -> "HK.00700" (already Futu)
        "AAPL"     -> "US.AAPL"  (assumes US for pure codes)
        "00700"    -> "HK.00700" (assumes HK for numeric codes)
    """
    if not code:
        return code
    
    code = code.strip().upper()
    
    # Already Futu format: MARKET.CODE
    if _is_futu(code):
        return code
    
    # Canonical format: CODE.MARKET
    if _is_canonical(code):
        code_part, market = code.split(".")
        return f"{market}.{code_part}"
    
    # Pure code - infer market
    return _infer_and_format(code)


def _is_futu(code: str) -> bool:
    """Check if code is in Futu format (MARKET.CODE)."""
    parts = code.split(".")
    if len(parts) != 2:
        return False
    market, code_part = parts
    return market in ("HK", "US", "SH", "SZ") and len(code_part) > 0


def _infer_and_format(code: str) -> str:
    """Infer market from pure code and return Futu format."""
    # Numeric codes are HK stocks
    if code.isdigit():
        return f"HK.{code}"
    # Alpha codes are US stocks
    return f"US.{code}"


# ── Pure Code (no market) ────────────────────────────────────────────────────

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
    
    code = code.strip().upper()
    
    # Remove MARKET. prefix
    for prefix in ("HK.", "US.", "SH.", "SZ."):
        if code.startswith(prefix):
            return code[len(prefix):]
    
    # Remove .MARKET suffix
    if _is_canonical(code):
        return code.split(".")[0]
    
    return code


# ── Market Detection ─────────────────────────────────────────────────────────

def detect_market(code: str) -> str | None:
    """Detect market from any code format.
    
    Returns: "HK", "US", "SH", "SZ", or None if unknown.
    
    Examples:
        "00700.HK" -> "HK"
        "HK.00700" -> "HK"
        "AAPL.US"  -> "US"
        "US.AAPL"  -> "US"
        "AAPL"     -> "US"  (inferred)
        "00700"    -> "HK"  (inferred)
    """
    if not code:
        return None
    
    code = code.strip().upper()
    
    # Canonical: CODE.MARKET
    if _is_canonical(code):
        return code.split(".")[1]
    
    # Futu: MARKET.CODE
    if _is_futu(code):
        return code.split(".")[0]
    
    # Pure code - infer
    if code.isdigit():
        return "HK"
    if code.isalpha():
        return "US"
    
    return None


# ── Display Format ───────────────────────────────────────────────────────────

def to_display(code: str) -> str:
    """Convert to display format (CODE.MARKET) for UI.
    
    Same as canonical, but handles pure codes by appending inferred market.
    
    Examples:
        "HK.00700" -> "00700.HK"
        "AAPL.US"  -> "AAPL.US"
        "AAPL"     -> "AAPL.US"  (inferred)
        "00700"    -> "00700.HK" (inferred)
    """
    canonical = to_canonical(code)
    
    # If already has market suffix, return as-is
    if _is_canonical(canonical):
        return canonical
    
    # Pure code - infer market and append
    market = detect_market(code)
    if market:
        return f"{canonical}.{market}"
    
    return canonical


# ── Validation ───────────────────────────────────────────────────────────────

def is_valid_code(code: str) -> bool:
    """Check if code is a valid stock code in any format."""
    if not code:
        return False
    
    code = code.strip().upper()
    
    # Check all formats
    if _is_canonical(code) or _is_futu(code):
        return True
    
    # Pure code - must be alphanumeric
    return code.isalnum() and len(code) >= 4


# ── Batch Conversion ─────────────────────────────────────────────────────────

def to_canonical_batch(codes: list[str]) -> list[str]:
    """Convert list of codes to canonical format."""
    return [to_canonical(c) for c in codes]


def to_futu_batch(codes: list[str]) -> list[str]:
    """Convert list of codes to Futu format."""
    return [to_futu(c) for c in codes]
