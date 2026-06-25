"""Quick test runner for test_futu_provider_enhanced.py."""
import sys
sys.path.insert(0, '.')

from tests.test_futu_provider_enhanced import TestPanelCache, TestValidateOHLC, TestGetPanelDataCache

# Run PanelCache tests
print("Running PanelCache tests...")
t = TestPanelCache()
t.test_cache_miss_on_empty(); print("  ✓ cache_miss_on_empty")
t.test_cache_hit_same_day(); print("  ✓ cache_hit_same_day")
t.test_cache_miss_different_symbols(); print("  ✓ cache_miss_different_symbols")
t.test_cache_miss_different_dates(); print("  ✓ cache_miss_different_dates")
t.test_cache_miss_different_autype(); print("  ✓ cache_miss_different_autype")
t.test_cache_expiry_cross_day(); print("  ✓ cache_expiry_cross_day")
t.test_cache_clear(); print("  ✓ cache_clear")
t.test_cache_symbol_order_independent(); print("  ✓ cache_symbol_order_independent")
print("All PanelCache tests passed!\n")

# Run ValidateOHLC tests
print("Running ValidateOHLC tests...")
t2 = TestValidateOHLC()
t2.test_valid_data(); print("  ✓ valid_data")
t2.test_nan_values(); print("  ✓ nan_values")
t2.test_invalid_price_close_zero(); print("  ✓ invalid_price_close_zero")
t2.test_invalid_price_high_less_than_low(); print("  ✓ invalid_price_high_less_than_low")
t2.test_zero_volume(); print("  ✓ zero_volume")
t2.test_price_jump(); print("  ✓ price_jump")
t2.test_drop_strategy(); print("  ✓ drop_strategy")
t2.test_raise_strategy(); print("  ✓ raise_strategy")
t2.test_empty_dataframe(); print("  ✓ empty_dataframe")
t2.test_missing_columns(); print("  ✓ missing_columns")
t2.test_repair_suggestions(); print("  ✓ repair_suggestions")
print("All ValidateOHLC tests passed!\n")

print("=== ALL TESTS PASSED ===")
