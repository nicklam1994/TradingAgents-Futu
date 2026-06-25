# -*- coding: utf-8 -*-
"""Shadow CodeGen + Backtester tests.

Covers:
- codegen: ShadowRule → signal_engine.py source generation + validation
- codegen: build_config() → config.json dict
- codegen: write_run_dir() → full run_dir materialization
- backtester: select_multi_market_codes() + flatten_codes()
- backtester: run_shadow_backtest() with mock backtest fn
- backtester: attribution computation with mock roundtrips
- backtester: load_cached_result() / _cache_result()
- Integration: ShadowAccountTool 5-step pipeline (extract→scan→codegen→backtest→attribution)
"""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from tradingagents.shadow.models import (
    AttributionBreakdown,
    ShadowBacktestResult,
    ShadowProfile,
    ShadowRule,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def sample_rule() -> ShadowRule:
    """A single HK momentum rule."""
    return ShadowRule(
        rule_id="R1",
        human_text="港股放量突破",
        entry_condition={"market": "HK", "entry_hour": {"min": 9, "max": 16}},
        exit_condition={"stop_loss_pct": -5.0},
        holding_days_range=(3, 7),
        support_count=12,
        coverage_rate=0.4,
        sample_trades=("HK.00700@2024-01-15",),
        weight=1.0,
    )


@pytest.fixture
def sample_rule_us() -> ShadowRule:
    """A US tech momentum rule."""
    return ShadowRule(
        rule_id="R2",
        human_text="US tech breakout",
        entry_condition={"market": "US"},
        exit_condition={"take_profit_pct": 15.0},
        holding_days_range=(5, 14),
        support_count=8,
        coverage_rate=0.3,
        sample_trades=("AAPL@2024-03-01",),
        weight=0.8,
    )


@pytest.fixture
def sample_profile(sample_rule: ShadowRule, sample_rule_us: ShadowRule) -> ShadowProfile:
    """A complete shadow profile with two rules."""
    return ShadowProfile(
        shadow_id="test_shadow_001",
        created_at="2024-06-01T00:00:00",
        journal_hash="abc12345",
        source_market="HK",
        profitable_roundtrips=20,
        total_roundtrips=35,
        date_range=("2024-01-01", "2024-06-01"),
        profile_text="偏好港股短线动量，辅以美股科技趋势",
        rules=(sample_rule, sample_rule_us),
        preferred_markets=("HK", "US"),
        typical_holding_days=(5.0, 10.0),
    )


@pytest.fixture
def sample_trades_df() -> pd.DataFrame:
    """A small trades DataFrame for attribution testing."""
    return pd.DataFrame([
        {"symbol": "HK.00700", "datetime": pd.Timestamp("2024-01-10"), "side": "buy", "price": 350.0, "qty": 100},
        {"symbol": "HK.00700", "datetime": pd.Timestamp("2024-01-15"), "side": "sell", "price": 370.0, "qty": 100},
        {"symbol": "AAPL", "datetime": pd.Timestamp("2024-02-01"), "side": "buy", "price": 180.0, "qty": 50},
        {"symbol": "AAPL", "datetime": pd.Timestamp("2024-02-20"), "side": "sell", "price": 175.0, "qty": 50},
    ])


# ── CodeGen tests ──────────────────────────────────────────────────────────


class TestRenderSignalEngine:
    """Test ShadowRule → signal_engine.py source generation."""

    def test_renders_valid_python(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.codegen import render_signal_engine, validate_generated

        source = render_signal_engine(sample_profile)
        assert isinstance(source, str)
        assert len(source) > 100

        ok, err = validate_generated(source)
        assert ok, f"Generated source failed validation: {err}"

    def test_contains_shadow_id(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.codegen import render_signal_engine

        source = render_signal_engine(sample_profile)
        assert "test_shadow_001" in source

    def test_contains_rules(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.codegen import render_signal_engine

        source = render_signal_engine(sample_profile)
        assert "R1" in source
        assert "R2" in source

    def test_has_signal_engine_class(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.codegen import render_signal_engine

        source = render_signal_engine(sample_profile)
        tree = ast.parse(source)

        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        assert any(c.name == "SignalEngine" for c in classes)

    def test_has_generate_method(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.codegen import render_signal_engine

        source = render_signal_engine(sample_profile)
        tree = ast.parse(source)

        signal_cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "SignalEngine")
        methods = [n.name for n in signal_cls.body if isinstance(n, ast.FunctionDef)]
        assert "generate" in methods

    def test_market_detection_hk_us_only(self, sample_profile: ShadowProfile):
        """Verify the template's _market_of only handles HK/US."""
        from tradingagents.shadow.codegen import render_signal_engine

        source = render_signal_engine(sample_profile)
        # Should NOT contain china_a or crypto
        assert "china_a" not in source
        assert "crypto" not in source


class TestValidateGenerated:
    """Test static validation of generated source."""

    def test_rejects_syntax_error(self):
        from tradingagents.shadow.codegen import validate_generated

        ok, err = validate_generated("def foo(:")
        assert not ok
        assert "SyntaxError" in err

    def test_rejects_missing_class(self):
        from tradingagents.shadow.codegen import validate_generated

        ok, err = validate_generated("x = 1\n")
        assert not ok
        assert "SignalEngine" in err

    def test_rejects_missing_generate(self):
        from tradingagents.shadow.codegen import validate_generated

        source = """
class SignalEngine:
    def run(self):
        pass
"""
        ok, err = validate_generated(source)
        assert not ok
        assert "generate" in err

    def test_rejects_no_data_map_arg(self):
        from tradingagents.shadow.codegen import validate_generated

        source = """
class SignalEngine:
    def generate(self):
        pass
"""
        ok, err = validate_generated(source)
        assert not ok
        assert "data_map" in err


class TestBuildConfig:
    """Test config.json generation."""

    def test_basic_config(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.codegen import build_config

        cfg = build_config(
            sample_profile,
            codes=["HK.00700", "AAPL"],
            start_date="2024-01-01",
            end_date="2024-06-01",
        )
        assert cfg["shadow_id"] == "test_shadow_001"
        assert cfg["codes"] == ["HK.00700", "AAPL"]
        assert cfg["start_date"] == "2024-01-01"
        assert cfg["strategy_name"] == "shadow_test_shadow_001"
        assert cfg["engine"] == "daily"

    def test_config_with_extra(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.codegen import build_config

        cfg = build_config(
            sample_profile,
            codes=["AAPL"],
            start_date="2024-01-01",
            end_date="2024-06-01",
            extra={"custom_field": 42},
        )
        assert cfg["custom_field"] == 42


class TestWriteRunDir:
    """Test run_dir materialization."""

    def test_creates_files(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.codegen import write_run_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = write_run_dir(
                sample_profile,
                tmpdir,
                codes=["HK.00700", "AAPL"],
                start_date="2024-01-01",
                end_date="2024-06-01",
            )
            assert (run_dir / "code" / "signal_engine.py").exists()
            assert (run_dir / "config.json").exists()

            # Verify signal_engine.py is valid
            source = (run_dir / "code" / "signal_engine.py").read_text()
            tree = ast.parse(source)
            assert any(
                isinstance(n, ast.ClassDef) and n.name == "SignalEngine"
                for n in ast.walk(tree)
            )

            # Verify config.json is valid JSON
            cfg = json.loads((run_dir / "config.json").read_text())
            assert cfg["shadow_id"] == "test_shadow_001"

    def test_rejects_invalid_profile(self):
        """A profile with no rules should still generate valid code."""
        from tradingagents.shadow.codegen import write_run_dir

        empty_profile = ShadowProfile(
            shadow_id="empty",
            created_at="2024-01-01T00:00:00",
            journal_hash="deadbeef",
            source_market="HK",
            profitable_roundtrips=0,
            total_roundtrips=0,
            date_range=("2024-01-01", "2024-06-01"),
            profile_text="empty",
            rules=(),
            preferred_markets=("HK",),
            typical_holding_days=(1.0, 5.0),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            # Empty rules → template still renders (RULES = [])
            run_dir = write_run_dir(
                empty_profile,
                tmpdir,
                codes=["HK.00700"],
                start_date="2024-01-01",
                end_date="2024-06-01",
            )
            assert (run_dir / "code" / "signal_engine.py").exists()


# ── Backtester tests ───────────────────────────────────────────────────────


class TestSelectCodes:
    """Test multi-market code selection."""

    def test_hk_and_us(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.backtester import select_multi_market_codes

        selection = select_multi_market_codes(sample_profile)
        assert "HK" in selection
        assert "US" in selection
        assert len(selection["HK"]) > 0
        assert len(selection["US"]) > 0

    def test_per_market_count(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.backtester import select_multi_market_codes

        selection = select_multi_market_codes(sample_profile, per_market_count=2)
        assert len(selection["HK"]) == 2
        assert len(selection["US"]) == 2

    def test_flatten_codes(self):
        from tradingagents.shadow.backtester import flatten_codes

        selection = {"HK": ["HK.00700", "HK.09988"], "US": ["AAPL", "MSFT"]}
        codes = flatten_codes(selection)
        assert codes == ["HK.00700", "HK.09988", "AAPL", "MSFT"]

    def test_flatten_deduplicates(self):
        from tradingagents.shadow.backtester import flatten_codes

        selection = {"HK": ["AAPL"], "US": ["AAPL", "MSFT"]}
        codes = flatten_codes(selection)
        assert codes.count("AAPL") == 1


class TestRunShadowBacktest:
    """Test the backtest driver with a mock backtest function."""

    def test_basic_run(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.backtester import run_shadow_backtest

        def mock_backtest(run_dir_str: str) -> str:
            return json.dumps({
                "status": "ok",
                "artifacts": {},
                "stderr": "",
            })

        result = run_shadow_backtest(
            sample_profile,
            window_start="2024-01-01",
            window_end="2024-06-01",
            run_backtest_fn=mock_backtest,
        )
        assert isinstance(result, ShadowBacktestResult)
        assert result.shadow_id == "test_shadow_001"
        assert "HK" in result.per_market
        assert "US" in result.per_market

    def test_with_metrics(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.backtester import run_shadow_backtest

        def mock_backtest(run_dir_str: str) -> str:
            # Write a metrics file
            run_dir = Path(run_dir_str)
            metrics = {"total_return_abs": 15000.0, "sharpe_ratio": 1.5}
            (run_dir / "metrics.json").write_text(json.dumps(metrics))
            return json.dumps({
                "status": "ok",
                "artifacts": {"metrics.json": str(run_dir / "metrics.json")},
            })

        result = run_shadow_backtest(
            sample_profile,
            window_start="2024-01-01",
            window_end="2024-06-01",
            run_backtest_fn=mock_backtest,
        )
        assert result.combined.get("total_return_abs") == 15000.0
        assert result.shadow_total_pnl == 15000.0

    def test_with_journal_attribution(self, sample_profile: ShadowProfile, sample_trades_df: pd.DataFrame):
        from tradingagents.shadow.backtester import run_shadow_backtest

        def mock_backtest(run_dir_str: str) -> str:
            run_dir = Path(run_dir_str)
            metrics = {"total_return_abs": 5000.0}
            (run_dir / "metrics.json").write_text(json.dumps(metrics))
            return json.dumps({
                "status": "ok",
                "artifacts": {"metrics.json": str(run_dir / "metrics.json")},
            })

        result = run_shadow_backtest(
            sample_profile,
            window_start="2024-01-01",
            window_end="2024-06-01",
            journal_df=sample_trades_df,
            run_backtest_fn=mock_backtest,
        )
        # Attribution should be computed (not all zeros)
        assert isinstance(result.attribution, AttributionBreakdown)
        assert result.real_total_pnl != 0.0  # trades had PnL

    def test_empty_codes_raises(self):
        from tradingagents.shadow.backtester import run_shadow_backtest

        empty_profile = ShadowProfile(
            shadow_id="empty",
            created_at="2024-01-01T00:00:00",
            journal_hash="x",
            source_market="HK",
            profitable_roundtrips=0,
            total_roundtrips=0,
            date_range=("2024-01-01", "2024-06-01"),
            profile_text="",
            rules=(),
            preferred_markets=(),
            typical_holding_days=(1.0, 5.0),
        )
        with pytest.raises(ValueError, match="No codes"):
            run_shadow_backtest(
                empty_profile,
                window_start="2024-01-01",
                window_end="2024-06-01",
                markets=(),  # no markets → no codes
                run_backtest_fn=lambda s: '{"status":"ok","artifacts":{}}',
            )


class TestCachedResult:
    """Test result caching / loading."""

    def test_cache_and_load(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.backtester import _cache_result, load_cached_result, runs_dir

        run_dir = runs_dir(sample_profile.shadow_id)
        result = ShadowBacktestResult(
            shadow_id=sample_profile.shadow_id,
            per_market={"HK": {"sharpe": 1.2}},
            combined={"sharpe": 1.2},
            equity_curves={},
            attribution=AttributionBreakdown(
                missed_signals_pnl=100.0,
                noise_trades_pnl=-50.0,
                early_exit_pnl=20.0,
                late_exit_pnl=-10.0,
                overtrading_pnl=-5.0,
            ),
            shadow_total_pnl=5000.0,
            real_total_pnl=4945.0,
            delta_pnl=55.0,
        )
        _cache_result(run_dir, result)

        loaded = load_cached_result(sample_profile.shadow_id)
        assert loaded is not None
        assert loaded.shadow_id == sample_profile.shadow_id
        assert loaded.attribution.missed_signals_pnl == 100.0
        assert loaded.shadow_total_pnl == 5000.0

    def test_load_missing_returns_none(self):
        from tradingagents.shadow.backtester import load_cached_result

        assert load_cached_result("nonexistent_shadow_xyz") is None


class TestAttribution:
    """Test the attribution computation logic."""

    def test_compute_attribution_basic(self, sample_profile: ShadowProfile):
        from tradingagents.shadow.backtester import _compute_attribution

        roundtrips = [
            {"symbol": "HK.00700", "buy_dt": "2024-01-10", "sell_dt": "2024-01-15",
             "hold_days": 5, "pnl": 2000.0},
            {"symbol": "HK.00700", "buy_dt": "2024-02-01", "sell_dt": "2024-02-03",
             "hold_days": 2, "pnl": -500.0},  # below rule_hold_lo=3
        ]
        attr, shadow_pnl, real_pnl = _compute_attribution(
            profile=sample_profile,
            roundtrips=roundtrips,
            shadow_pnl=10000.0,
        )
        assert isinstance(attr, AttributionBreakdown)
        assert real_pnl == 1500.0  # 2000 + (-500)

    def test_zero_attribution(self):
        from tradingagents.shadow.backtester import _zero_attribution

        attr = _zero_attribution()
        assert attr.missed_signals_pnl == 0.0
        assert attr.noise_trades_pnl == 0.0
        assert attr.counterfactual_trades == ()


# ── Integration: 5-step pipeline ──────────────────────────────────────────


class TestShadowPipeline:
    """Verify the 5-step ShadowAccountTool pipeline is structurally complete.

    extract → scan → codegen → backtest → attribution

    This test only checks that the functions exist and are importable,
    not that they produce correct output (covered by unit tests above).
    """

    def test_pipeline_importable(self):
        from tradingagents.shadow import (
            extract_shadow_profile,
            scan_today_signals,
            render_signal_engine,
            validate_generated,
            build_config,
            write_run_dir,
            run_shadow_backtest,
            compute_attribution,
        )
        # All 5 stages must be callable
        assert callable(extract_shadow_profile)
        assert callable(scan_today_signals)
        assert callable(render_signal_engine)
        assert callable(validate_generated)
        assert callable(build_config)
        assert callable(write_run_dir)
        assert callable(run_shadow_backtest)
        assert callable(compute_attribution)

    def test_codegen_to_backtest_integration(self, sample_profile: ShadowProfile):
        """Full round-trip: codegen → write_run_dir → mock backtest → result."""
        from tradingagents.shadow.codegen import write_run_dir
        from tradingagents.shadow.backtester import run_shadow_backtest

        def mock_backtest(run_dir_str: str) -> str:
            run_dir = Path(run_dir_str)
            metrics = {"total_return_abs": 8000.0, "sharpe_ratio": 1.1}
            (run_dir / "metrics.json").write_text(json.dumps(metrics))
            return json.dumps({
                "status": "ok",
                "artifacts": {"metrics.json": str(run_dir / "metrics.json")},
            })

        result = run_shadow_backtest(
            sample_profile,
            window_start="2024-01-01",
            window_end="2024-06-01",
            run_backtest_fn=mock_backtest,
        )
        assert result.shadow_id == "test_shadow_001"
        assert result.combined["total_return_abs"] == 8000.0
        assert result.shadow_total_pnl == 8000.0
