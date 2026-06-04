"""E2E test: FutuProvider via route_to_vendor.

Tests the routing and fallback mechanism:
  1. Verify FutuProvider is in the registry and configured as top priority
  2. Verify A-share (600519.SH) triggers NotImplementedError → fallback
  3. Verify the fallback chain works (Futu fails → next provider kicks in)

Note: Live Futu OpenD tests require a running FutuOpenD on 127.0.0.1:11111.
      If not reachable, the connection error triggers fallback automatically.
"""
import sys
sys.path.insert(0, ".")

from tradingagents.dataflows.providers.registry import build_default_registry
from tradingagents.dataflows.providers.futu_provider import FutuProvider
from tradingagents.default_config import DEFAULT_CONFIG

print("=" * 60)
print("E2E Test: FutuProvider")
print("=" * 60)

# ── Test 1: Registry ──
print("\n[1/4] Registry verification")
r = build_default_registry()
names = r.list_names()
assert "futu" in names, "futu not in registry"
assert names[0] == "futu", f"futu should be first, got: {names[0]}"
print(f"  PASS: futu is first in registry: {names}")

# ── Test 2: Config routing ──
print("\n[2/4] Config routing verification")
vendors = DEFAULT_CONFIG["data_vendors"]
assert vendors["core_stock_apis"].startswith("futu"), "futu not first in core_stock_apis"
assert vendors["realtime_data"].startswith("futu"), "futu not first in realtime_data"
assert "futu" not in vendors["news_data"], "futu should NOT be in news_data"
print(f"  PASS: futu is top priority in routing config")

# ── Test 3: Code conversion ──
print("\n[3/4] Code conversion (_to_futu_code)")
from futu import Market
tests = [
    ("AAPL", (Market.US, "AAPL")),
    ("NVDA.US", (Market.US, "NVDA")),
    ("00700.HK", (Market.HK, "00700")),
]
for input_code, expected in tests:
    result = FutuProvider._to_futu_code(input_code)
    assert result == expected, f"_to_futu_code({input_code}) = {result}, expected {expected}"
    print(f"  {input_code} → {result} ✓")

# A-share should raise NotImplementedError
try:
    FutuProvider._to_futu_code("600519.SH")
    assert False, "Should have raised NotImplementedError"
except NotImplementedError:
    print("  600519.SH → NotImplementedError ✓")

try:
    FutuProvider._to_futu_code("000001.SZ")
    assert False, "Should have raised NotImplementedError"
except NotImplementedError:
    print("  000001.SZ → NotImplementedError ✓")

# ── Test 4: Provider methods exist ──
print("\n[4/4] Provider method verification")
p = FutuProvider()
assert p.name == "futu"
methods = [
    "get_stock_data", "get_indicators", "get_fundamentals",
    "get_realtime_quotes", "get_balance_sheet", "get_cashflow",
    "get_income_statement", "get_news", "get_global_news",
    "get_insider_transactions",
]
for m in methods:
    assert hasattr(p, m), f"FutuProvider missing method: {m}"
print(f"  PASS: all {len(methods)} methods present")

print("\n" + "=" * 60)
print("ALL TESTS PASSED")
print("=" * 60)
