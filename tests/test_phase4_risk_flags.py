"""Tests for Phase 4: structured output & risk control enhancements."""

from api.services.report_service import resolve_report_fields, StructuredReport
from tradingagents.graph.signal_processing import extract_verdict_data, extract_risk_judge_data


def test_extract_verdict_data_full():
    """extract_verdict_data parses all VERDICT JSON fields."""
    text = (
        "Analysis...\n"
        '<!-- VERDICT: {"direction": "bullish", "reason": "strong fundamentals", '
        '"confidence": 0.85, "signal": "bullish", '
        '"key_levels": {"support": 150.0, "resistance": 180.0}, '
        '"target_price": 175.0, "risk_flags": ["high_volatility", "liquidity_risk"]} -->'
        "\nMore text"
    )
    r = extract_verdict_data(text)
    assert r["confidence"] == 0.85
    assert r["signal"] == "bullish"
    assert r["key_levels"] == {"support": 150.0, "resistance": 180.0}
    assert r["target_price"] == 175.0
    assert r["risk_flags"] == ["high_volatility", "liquidity_risk"]
    assert r["direction"] == "bullish"


def test_extract_verdict_data_empty():
    """extract_verdict_data returns empty dict on missing/invalid VERDICT."""
    assert extract_verdict_data("") == {}
    assert extract_verdict_data("no verdict here") == {}
    assert extract_verdict_data("<!-- VERDICT: not json -->") == {}


def test_extract_risk_judge_data_full():
    """extract_risk_judge_data parses RISK_JUDGE block."""
    text = (
        "Risk assessment\n"
        '<!-- RISK_JUDGE: {"verdict": "pass", "revision_reason": "", '
        '"risk_flags": ["volatility_risk", "event_risk", "macro_risk"]} -->'
    )
    r = extract_risk_judge_data(text)
    assert r["verdict"] == "pass"
    assert r["risk_flags"] == ["volatility_risk", "event_risk", "macro_risk"]


def test_extract_risk_judge_data_empty():
    """extract_risk_judge_data returns empty dict on missing RISK_JUDGE."""
    assert extract_risk_judge_data("") == {}
    assert extract_risk_judge_data("no risk judge") == {}


def test_resolve_report_fields_includes_risk_flags():
    """resolve_report_fields returns aggregated risk_flags from VERDICT + RISK_JUDGE."""
    text = (
        "Final decision: BUY\n"
        '<!-- VERDICT: {"direction": "bullish", "reason": "test", '
        '"confidence": 0.7, "risk_flags": ["high_volatility"]} -->\n'
        '<!-- RISK_JUDGE: {"verdict": "pass", '
        '"risk_flags": ["event_risk", "high_volatility"]} -->'
    )
    resolved = resolve_report_fields(result_data={"final_trade_decision": text})
    assert "risk_flags" in resolved, "risk_flags missing from resolved dict"
    assert "high_volatility" in resolved["risk_flags"]
    assert "event_risk" in resolved["risk_flags"]
    # Dedup: high_volatility in both VERDICT and RISK_JUDGE, should appear once
    assert resolved["risk_flags"].count("high_volatility") == 1


def test_resolve_report_fields_verdict_confidence_fallback():
    """Confidence falls back to VERDICT data when regex finds nothing."""
    text = (
        "Some report without explicit confidence\n"
        '<!-- VERDICT: {"direction": "bullish", "reason": "x", "confidence": 0.65} -->'
    )
    resolved = resolve_report_fields(result_data={"final_trade_decision": text})
    assert resolved["confidence"] == 65  # 0.65 * 100, rounded


def test_resolve_report_fields_verdict_target_price_fallback():
    """Target price falls back to VERDICT target_price."""
    text = (
        "Report\n"
        '<!-- VERDICT: {"direction": "bullish", "reason": "x", "target_price": 175.5} -->'
    )
    resolved = resolve_report_fields(result_data={"final_trade_decision": text})
    assert resolved["target_price"] == 175.5


def test_structured_report_has_risk_flags():
    """StructuredReport model includes risk_flags field."""
    sr = StructuredReport(decision="BUY", risk_flags=["test_flag"])
    assert sr.risk_flags == ["test_flag"]


def test_structured_report_risk_flags_default_empty():
    """StructuredReport.risk_flags defaults to empty list."""
    sr = StructuredReport(decision="HOLD")
    assert sr.risk_flags == []


if __name__ == "__main__":
    test_extract_verdict_data_full()
    test_extract_verdict_data_empty()
    test_extract_risk_judge_data_full()
    test_extract_risk_judge_data_empty()
    test_resolve_report_fields_includes_risk_flags()
    test_resolve_report_fields_verdict_confidence_fallback()
    test_resolve_report_fields_verdict_target_price_fallback()
    test_structured_report_has_risk_flags()
    test_structured_report_risk_flags_default_empty()
    print("ALL 9 TESTS PASSED")
