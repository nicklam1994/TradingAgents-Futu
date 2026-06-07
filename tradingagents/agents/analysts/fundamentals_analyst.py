import asyncio

from langchain_core.messages import HumanMessage, SystemMessage
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt
from tradingagents.graph.intent_parser import build_horizon_context
from tradingagents.agents.utils.agent_states import current_tracker_var, extract_verdict


def create_fundamentals_analyst(llm, data_collector=None):
    async def _safe(tool, payload):
        try:
            return await asyncio.to_thread(tool.invoke, payload)
        except Exception as exc:
            return f"调用失败：{exc}"

    def _has_data(s):
        """Check if a data string is valid (not error/empty/too short)."""
        if not s or not isinstance(s, str):
            return False
        s_low = s.lower()
        if "error" in s_low or "no data" in s_low or "no financial" in s_low:
            return False
        return len(s) > 50

    async def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        print(f"[Fundamentals Analyst] START {ticker} {current_date}")
        horizon = state.get("horizon", "short")
        user_intent = state.get("user_intent") or {}
        focus_areas = user_intent.get("focus_areas", [])
        specific_questions = user_intent.get("specific_questions", [])

        config = get_config()
        system_message = get_prompt("fundamentals_system_message", config=config)
        horizon_ctx = build_horizon_context(horizon, focus_areas, specific_questions, agent_type="fundamentals")

        from tradingagents.agents.utils.agent_utils import (
            get_fundamentals, get_financial_report, get_analyst_consensus,
            get_revenue_breakdown, get_financial_alerts,
        )

        pool = data_collector.get(ticker, current_date) if data_collector else None

        # Always fetch Futu financial reports (primary source)
        futu_income, futu_balance, futu_cashflow, fundamentals = await asyncio.gather(
            _safe(get_financial_report, {"symbol": ticker, "report_type": "income"}),
            _safe(get_financial_report, {"symbol": ticker, "report_type": "balance"}),
            _safe(get_financial_report, {"symbol": ticker, "report_type": "cashflow"}),
            _safe(get_fundamentals, {"ticker": ticker, "curr_date": current_date}),
        )

        # yfinance fallback only when Futu fails
        if not _has_data(futu_income):
            from tradingagents.agents.utils.agent_utils import get_income_statement
            pool_income = pool.get("income_statement", "") if pool else ""
            futu_income = pool_income if _has_data(pool_income) else await _safe(get_income_statement, {"ticker": ticker, "freq": "quarterly", "curr_date": current_date})
        if not _has_data(futu_balance):
            from tradingagents.agents.utils.agent_utils import get_balance_sheet
            pool_balance = pool.get("balance_sheet", "") if pool else ""
            futu_balance = pool_balance if _has_data(pool_balance) else await _safe(get_balance_sheet, {"ticker": ticker, "freq": "quarterly", "curr_date": current_date})
        if not _has_data(futu_cashflow):
            from tradingagents.agents.utils.agent_utils import get_cashflow
            pool_cashflow = pool.get("cashflow", "") if pool else ""
            futu_cashflow = pool_cashflow if _has_data(pool_cashflow) else await _safe(get_cashflow, {"ticker": ticker, "freq": "quarterly", "curr_date": current_date})

        income = futu_income or "利润表数据缺失"
        balance = futu_balance or "资产负债表数据缺失"
        cashflow = futu_cashflow or "现金流量表数据缺失" 

        # Always fresh: analyst consensus + revenue breakdown + financial alerts + dividends
        from tradingagents.agents.utils.agent_utils import get_dividend_history
        consensus, revenue, alerts, dividends = await asyncio.gather(
            _safe(get_analyst_consensus, {"symbol": ticker}),
            _safe(get_revenue_breakdown, {"symbol": ticker}),
            _safe(get_financial_alerts, {"symbol": ticker}),
            _safe(get_dividend_history, {"symbol": ticker}),
        )

        messages = [
            SystemMessage(content=system_message + "\n\n请全程使用中文。"),
            HumanMessage(content=(
                horizon_ctx + "\n"
                f"以下是 {ticker} 在 {current_date} 的基本面资料。\n\n"
                f"【基本面快照(PE/PB/市值/股息率)】\n{fundamentals}\n\n"
                f"【利润表】\n{income}\n\n"
                f"【资产负债表】\n{balance}\n\n"
                f"【现金流量表】\n{cashflow}\n\n"
                f"【营收分部(业务线+地区)】\n{revenue}\n\n"
                f"【分析师共识评级】\n{consensus}\n\n"
                f"【股息历史】\n{dividends}\n\n"
                f"【财务异常预警】\n{alerts}"
            )),
        ]

        # ── Token 级流式输出 ──────────────────
        tracker = current_tracker_var.get()
        full_content = ""
        async for chunk in llm.astream(messages):
            content = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += content
            if tracker:
                tracker._emit_token("Fundamentals Analyst", "fundamentals_report", content)

        verdict, confidence = extract_verdict(full_content)
        print(f"[Fundamentals Analyst] DONE {ticker}, report length={len(full_content)}")

        # ── 数据来源追溯 ──────────────────
        from tradingagents.agents.utils.data_trace import build_data_trace, summarize_data
        trace = build_data_trace("基本面分析师", [
            ("get_fundamentals", "Futu snapshot", summarize_data(fundamentals)),
            ("get_financial_report(income)", "Futu", summarize_data(income)),
            ("get_financial_report(balance)", "Futu", summarize_data(balance)),
            ("get_financial_report(cashflow)", "Futu", summarize_data(cashflow)),
            ("get_revenue_breakdown", "Futu", summarize_data(revenue)),
            ("get_analyst_consensus", "Futu", summarize_data(consensus)),
            ("get_dividend_history", "Futu", summarize_data(dividends)),
            ("get_financial_alerts", "Futu", summarize_data(alerts)),
        ])
        full_content += trace

        return {
            "fundamentals_report": full_content,
            "analyst_traces": [{
                "agent": "fundamentals_analyst",
                "horizon": horizon,
                "data_window": "财报周期",
                "key_finding": f"基本面分析结论：{verdict}",
                "verdict": verdict,
                "confidence": confidence,
            }],
        }

    return fundamentals_analyst_node
