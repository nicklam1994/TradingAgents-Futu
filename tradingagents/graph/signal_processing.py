# TradingAgents/graph/signal_processing.py

import re
import json

from langchain_openai import ChatOpenAI
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt


class SignalProcessor:
    """Processes trading signals to extract actionable decisions."""

    def __init__(self, quick_thinking_llm: ChatOpenAI):
        """Initialize with an LLM for processing."""
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, full_signal: str) -> str:
        """
        Process a full trading signal to extract the core decision.

        Args:
            full_signal: Complete trading signal text

        Returns:
            Extracted decision (BUY, SELL, or HOLD)
        """
        if not full_signal:
            return "HOLD"

        decision = _extract_decision_keyword(full_signal)
        if decision:
            return decision

        messages = [
            (
                "system",
                get_prompt("signal_extractor_system", config=get_config()),
            ),
            ("human", full_signal),
        ]

        response = str(self.quick_thinking_llm.invoke(messages).content).strip().upper()
        if response in {"BUY", "SELL", "HOLD"}:
            return response
        return "HOLD"


def _extract_decision_keyword(text: str) -> str | None:
    """Rule-based decision extraction to keep UI consistent with final decision text."""
    upper = text.upper()

    def parse_verdict_direction(raw_text: str) -> str | None:
        match = re.search(r"<!--\s*VERDICT:\s*(\{.*?\})\s*-->", raw_text, re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(1))
        except Exception:
            return None
        direction = str(payload.get("direction", "")).strip().upper()
        direction_map = {
            "看多": "BUY",
            "偏多": "BUY",
            "BULLISH": "BUY",
            "BUY": "BUY",
            "看空": "SELL",
            "偏空": "SELL",
            "BEARISH": "SELL",
            "SELL": "SELL",
            "中性": "HOLD",
            "NEUTRAL": "HOLD",
            "HOLD": "HOLD",
            "谨慎": "HOLD",
            "CAUTIOUS": "HOLD",
        }
        return direction_map.get(direction)

    def classify(snippet: str) -> str | None:
        snippet_upper = snippet.upper()
        sell_keywords = [
            "SELL",
            "卖出",
            "减持",
            "清仓",
            "空仓",
            "回避",
            "看空",
            "偏空",
        ]
        buy_keywords = [
            "BUY",
            "买入",
            "增持",
            "做多",
            "看多",
            "偏多",
            "谨慎看多",
            "有条件建仓",
            "条件建仓",
            "建仓",
        ]
        hold_keywords = [
            "HOLD",
            "观望",
            "持有",
            "中性",
        ]

        if any(k in snippet_upper for k in buy_keywords):
            return "BUY"
        if any(k in snippet_upper for k in sell_keywords):
            return "SELL"
        if any(k in snippet_upper for k in hold_keywords):
            return "HOLD"
        return None

    verdict_decision = parse_verdict_direction(text)
    if verdict_decision:
        return verdict_decision

    explicit_patterns = [
        r"最终裁决[:：]\s*([^\n*]+)",
        r"风控委员会最终裁决[:：]\s*([^\n*]+)",
        r"最终建议[:：]\s*([^\n*]+)",
        r"方向[:：]\s*([^\n*]+)",
        r"核心定性[:：]\s*([^\n*]+)",
    ]
    for pattern in explicit_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            decision = classify(match.group(1).strip())
            if decision:
                return decision

    headline = "\n".join(text.splitlines()[:20])
    decision = classify(headline)
    if decision:
        return decision

    decision = classify(upper)
    if decision:
        return decision

    return "UNKNOWN"


def extract_verdict_data(text: str) -> dict:
    """Extract structured VERDICT JSON from analyst report text.

    Parses the <!-- VERDICT: {...} --> block and returns all fields as a dict.
    Returns empty dict if no valid VERDICT found.

    Fields:
        direction: str (看多/偏多/中性/偏空/看空 or BULLISH/LEAN_BULLISH/etc.)
        reason: str
        confidence: float (0-1)
        signal: str (bullish/bearish/neutral)
        key_levels: dict {"support": float, "resistance": float}
        target_price: float or None
        risk_flags: list[str]
    """
    if not text:
        return {}

    match = re.search(
        r"<!--\s*VERDICT:\s*(\{.*?\})\s*-->",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return {}

    try:
        raw_json = match.group(1).strip().replace("\n", " ").replace("\r", " ")
        payload = json.loads(raw_json)
    except Exception:
        return {}

    result = {}

    # direction (existing field)
    direction = str(payload.get("direction") or "").strip()
    if direction:
        result["direction"] = direction

    # reason (existing field)
    reason = str(payload.get("reason") or "").strip()
    if reason:
        result["reason"] = reason

    # confidence (new: 0-1 float)
    conf = payload.get("confidence")
    if conf is not None:
        try:
            conf = float(conf)
            if 0 <= conf <= 1:
                result["confidence"] = conf
        except (ValueError, TypeError):
            pass

    # signal (new: bullish/bearish/neutral)
    signal = str(payload.get("signal") or "").strip().lower()
    if signal in ("bullish", "bearish", "neutral"):
        result["signal"] = signal

    # key_levels (new: {"support": float, "resistance": float})
    key_levels = payload.get("key_levels")
    if isinstance(key_levels, dict):
        support = key_levels.get("support")
        resistance = key_levels.get("resistance")
        try:
            support = float(support) if support is not None else 0.0
        except (ValueError, TypeError):
            support = 0.0
        try:
            resistance = float(resistance) if resistance is not None else 0.0
        except (ValueError, TypeError):
            resistance = 0.0
        if support > 0 or resistance > 0:
            result["key_levels"] = {"support": support, "resistance": resistance}

    # target_price (new: float or None)
    tp = payload.get("target_price")
    if tp is not None:
        try:
            tp = float(tp)
            if tp > 0:
                result["target_price"] = tp
        except (ValueError, TypeError):
            pass

    # risk_flags (new: list of risk tag strings)
    flags = payload.get("risk_flags")
    if isinstance(flags, list):
        valid_flags = {
            "high_volatility", "low_liquidity", "concentration_risk",
            "macro_risk", "event_risk", "technical_risk", "correlation_risk",
            "liquidity_risk", "volatility_risk",
        }
        result["risk_flags"] = [f for f in flags if isinstance(f, str) and f in valid_flags]

    return result


def extract_risk_judge_data(text: str) -> dict:
    """Extract structured RISK_JUDGE JSON from Risk Judge report text.

    Parses the <!-- RISK_JUDGE: {...} --> block and returns all fields as a dict.
    Returns empty dict if no valid RISK_JUDGE found.

    Fields:
        verdict: str (pass/revise/reject)
        revision_reason: str
        hard_constraints: list[str]
        soft_constraints: list[str]
        execution_preconditions: list[str]
        de_risk_triggers: list[str]
        risk_flags: list[str] (7-category risk tags)
    """
    if not text:
        return {}

    match = re.search(
        r"<!--\s*RISK_JUDGE:\s*(\{.*?\})\s*-->",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return {}

    try:
        raw_json = match.group(1).strip().replace("\n", " ").replace("\r", " ")
        payload = json.loads(raw_json)
    except Exception:
        return {}

    result = {}

    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict in ("pass", "revise", "reject"):
        result["verdict"] = verdict

    reason = str(payload.get("revision_reason") or "").strip()
    if reason:
        result["revision_reason"] = reason

    for key in ("hard_constraints", "soft_constraints", "execution_preconditions", "de_risk_triggers"):
        val = payload.get(key)
        if isinstance(val, list):
            result[key] = [str(v) for v in val if v]

    # risk_flags from RISK_JUDGE (7-category risk tags)
    flags = payload.get("risk_flags")
    if isinstance(flags, list):
        valid_flags = {
            "liquidity_risk", "volatility_risk", "concentration_risk",
            "correlation_risk", "macro_risk", "event_risk", "technical_risk",
        }
        result["risk_flags"] = [f for f in flags if isinstance(f, str) and f in valid_flags]

    return result
