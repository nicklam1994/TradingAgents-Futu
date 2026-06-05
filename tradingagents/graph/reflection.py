# TradingAgents/graph/reflection.py

from typing import Dict, Any
from langchain_openai import ChatOpenAI
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt


class Reflector:
    """Handles reflection on decisions and updating memory."""

    def __init__(self, quick_thinking_llm: ChatOpenAI):
        """Initialize the reflector with an LLM."""
        self.quick_thinking_llm = quick_thinking_llm
        self.reflection_system_prompt = self._get_reflection_prompt()

    def _get_reflection_prompt(self) -> str:
        """Get the system prompt for reflection."""
        return get_prompt("reflection_system_prompt", config=get_config())

    def _extract_current_situation(self, current_state: Dict[str, Any]) -> str:
        """Extract the current market situation from the state."""
        curr_market_report = current_state["market_report"]
        curr_sentiment_report = current_state["sentiment_report"]
        curr_news_report = current_state["news_report"]
        curr_fundamentals_report = current_state["fundamentals_report"]

        return f"{curr_market_report}\n\n{curr_sentiment_report}\n\n{curr_news_report}\n\n{curr_fundamentals_report}"

    def _reflect_on_component(
        self, component_type: str, report: str, situation: str, returns_losses
    ) -> str:
        """Generate reflection for a component."""
        messages = [
            ("system", self.reflection_system_prompt),
            (
                "human",
                f"Returns: {returns_losses}\n\nAnalysis/Decision: {report}\n\nObjective Market Reports for Reference: {situation}",
            ),
        ]

        result = self.quick_thinking_llm.invoke(messages).content
        return result

    def reflect_bull_researcher(self, current_state, returns_losses, bull_memory):
        """Reflect on bull researcher's analysis and update memory."""
        situation = self._extract_current_situation(current_state)
        bull_debate_history = current_state["investment_debate_state"]["bull_history"]

        result = self._reflect_on_component(
            "BULL", bull_debate_history, situation, returns_losses
        )
        bull_memory.add_situations([(situation, result)])

    def reflect_bear_researcher(self, current_state, returns_losses, bear_memory):
        """Reflect on bear researcher's analysis and update memory."""
        situation = self._extract_current_situation(current_state)
        bear_debate_history = current_state["investment_debate_state"]["bear_history"]

        result = self._reflect_on_component(
            "BEAR", bear_debate_history, situation, returns_losses
        )
        bear_memory.add_situations([(situation, result)])

    def reflect_trader(self, current_state, returns_losses, trader_memory):
        """Reflect on trader's decision and update memory."""
        situation = self._extract_current_situation(current_state)
        trader_decision = current_state["trader_investment_plan"]

        result = self._reflect_on_component(
            "TRADER", trader_decision, situation, returns_losses
        )
        trader_memory.add_situations([(situation, result)])

    def reflect_invest_judge(self, current_state, returns_losses, invest_judge_memory):
        """Reflect on investment judge's decision and update memory."""
        situation = self._extract_current_situation(current_state)
        judge_decision = current_state["investment_debate_state"]["judge_decision"]

        result = self._reflect_on_component(
            "INVEST JUDGE", judge_decision, situation, returns_losses
        )
        invest_judge_memory.add_situations([(situation, result)])

    def reflect_risk_manager(self, current_state, returns_losses, risk_manager_memory):
        """Reflect on risk manager's decision and update memory."""
        situation = self._extract_current_situation(current_state)
        judge_decision = current_state["risk_debate_state"]["judge_decision"]

        result = self._reflect_on_component(
            "RISK JUDGE", judge_decision, situation, returns_losses
        )
        risk_manager_memory.add_situations([(situation, result)])


# ── SimTradeReflector (Phase 7) ─────────────────────────────────────────────

import logging as _logging
from typing import Any, Dict, List, Optional, Tuple

from tradingagents.agents.utils.memory import FinancialSituationMemory

_reflector_logger = _logging.getLogger(__name__)

# System prompt for sim trade reflection — instructs the LLM to analyze a
# completed simulated trade and extract reusable lessons.
_SIM_TRADE_REFLECTION_PROMPT = """You are a trading reflection analyst. Review a completed simulated trade and extract concrete, reusable lessons.

For each trade you must:
1. Evaluate whether the original signal (buy/sell) was correct given the outcome.
2. Identify what the analysis got right and what it missed.
3. Assess whether the position sizing was appropriate.
4. Extract 2-3 concise, actionable lessons that would improve future trades in similar situations.

Output format (strict JSON):
{{
  "verdict": "good_trade" | "bad_trade" | "neutral",
  "what_was_right": "string",
  "what_was_wrong": "string",
  "sizing_assessment": "string",
  "lessons": ["lesson 1", "lesson 2", "lesson 3"],
  "lesson_summary": "One paragraph combining all lessons into a reusable insight."
}}

Be specific. Reference the actual symbol, prices, confidence, and outcome. Generic advice is useless."""


class SimTradeReflector:
    """Reflects on completed simulated trades and stores lessons in BM25 memory.

    Uses an LLM to generate structured reflections on trade outcomes, then
    stores the lessons in a FinancialSituationMemory (BM25) so they can be
    retrieved by similar future situations.

    Usage:
        reflector = SimTradeReflector(llm)
        lesson_text = reflector.reflect_on_sim_trade(trade_info, trade_result)
        # lesson_text is now stored in memory and can be retrieved later
    """

    def __init__(self, llm: ChatOpenAI):
        """Initialize with an LLM for generating reflections.

        Args:
            llm: A ChatOpenAI-compatible LLM instance (same as used by Reflector).
        """
        self.llm = llm
        # Dedicated BM25 memory for sim trade lessons
        self.memory = FinancialSituationMemory(name="sim_trade_lessons")

    def reflect_on_sim_trade(
        self,
        trade_info: Dict[str, Any],
        trade_result: Dict[str, Any],
    ) -> str:
        """Generate a reflection on a completed simulated trade.

        The trade_info dict should contain:
            - symbol: str (e.g., "HK.00700")
            - signal: str ("buy" or "sell")
            - confidence: float (0.0-1.0)
            - price: float (execution price)
            - quantity: int
            - reasoning: str (original analysis reasoning, optional)

        The trade_result dict should contain:
            - action_taken: str ("buy", "sell", "skipped")
            - order_id: str (if order was placed)
            - pnl: float (realized P&L, optional — 0 if not yet closed)
            - outcome: str ("win", "loss", "pending", optional)

        Args:
            trade_info: The original signal/trade parameters.
            trade_result: The execution result from SimExecutor/SimTradingService.

        Returns:
            A human-readable lesson summary string. Also stored in BM25 memory.
        """
        # Build the user message with trade details
        trade_context = self._format_trade_context(trade_info, trade_result)

        messages = [
            ("system", _SIM_TRADE_REFLECTION_PROMPT),
            ("human", trade_context),
        ]

        # Invoke LLM for reflection
        try:
            raw = self.llm.invoke(messages).content
            # content may be str or list — normalize to str
            result = raw if isinstance(raw, str) else str(raw)
        except Exception as e:
            _reflector_logger.error(
                "LLM reflection failed for %s: %s",
                trade_info.get("symbol", "unknown"),
                e,
                exc_info=True,
            )
            # Fallback: generate a basic lesson without LLM
            result = self._fallback_reflection(trade_info, trade_result)

        # Store in BM25 memory: (situation, recommendation) pair
        # Situation = trade summary, Recommendation = reflection lesson
        situation = self._build_situation_key(trade_info)
        try:
            self.memory.add_situations([(situation, result)])
            _reflector_logger.info(
                "Stored reflection for %s in BM25 memory (%d entries total)",
                trade_info.get("symbol", "unknown"),
                len(self.memory.documents),
            )
        except Exception as e:
            _reflector_logger.error(
                "Failed to store reflection in memory: %s", e, exc_info=True
            )

        return result

    def get_relevant_lessons(
        self, situation: str, n_matches: int = 3
    ) -> List[Dict[str, Any]]:
        """Retrieve past lessons relevant to a given situation.

        Args:
            situation: Description of the current market/trade situation.
            n_matches: Number of top matches to return.

        Returns:
            List of dicts with matched_situation, recommendation, similarity_score.
        """
        return self.memory.get_memories(situation, n_matches=n_matches)

    def _format_trade_context(
        self,
        trade_info: Dict[str, Any],
        trade_result: Dict[str, Any],
    ) -> str:
        """Format trade data into a prompt-friendly string."""
        parts = [
            "## Simulated Trade to Reflect On\n",
            f"**Symbol**: {trade_info.get('symbol', 'N/A')}",
            f"**Signal**: {trade_info.get('signal', 'N/A')}",
            f"**Confidence**: {trade_info.get('confidence', 'N/A')}",
            f"**Execution Price**: {trade_info.get('price', 'N/A')}",
            f"**Quantity**: {trade_info.get('quantity', 'N/A')}",
            f"**Action Taken**: {trade_result.get('action_taken', 'N/A')}",
            f"**Order ID**: {trade_result.get('order_id', 'N/A')}",
        ]

        # Optional fields
        if trade_info.get("reasoning"):
            parts.append(f"\n**Original Analysis Reasoning**:\n{trade_info['reasoning']}")

        if trade_result.get("pnl") is not None:
            parts.append(f"**Realized P&L**: {trade_result['pnl']}")

        if trade_result.get("outcome"):
            parts.append(f"**Outcome**: {trade_result['outcome']}")

        if trade_result.get("kelly_fraction") is not None:
            parts.append(f"**Kelly Fraction Used**: {trade_result['kelly_fraction']:.4f}")

        if trade_result.get("reason"):
            parts.append(f"\n**Execution Notes**: {trade_result['reason']}")

        parts.append("\nPlease reflect on this trade and extract lessons.")
        return "\n".join(parts)

    def _build_situation_key(self, trade_info: Dict[str, Any]) -> str:
        """Build a situation string for BM25 indexing.

        Captures the essential context so similar future trades can retrieve
        this reflection.
        """
        symbol = trade_info.get("symbol", "unknown")
        signal = trade_info.get("signal", "unknown")
        confidence = trade_info.get("confidence", 0)
        price = trade_info.get("price", 0)
        return (
            f"Simulated {signal} trade on {symbol} "
            f"at price {price} with confidence {confidence:.2f}"
        )

    def _fallback_reflection(
        self,
        trade_info: Dict[str, Any],
        trade_result: Dict[str, Any],
    ) -> str:
        """Generate a basic reflection without LLM when API calls fail."""
        symbol = trade_info.get("symbol", "unknown")
        signal = trade_info.get("signal", "unknown")
        action = trade_result.get("action_taken", "unknown")
        reason = trade_result.get("reason", "")

        return (
            f"Trade reflection for {symbol} ({signal}): "
            f"Action taken was {action}. "
            f"{reason}. "
            "LLM was unavailable for detailed analysis — review this trade manually."
        )
