import asyncio

from langchain_core.messages import HumanMessage, SystemMessage
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt
from tradingagents.graph.intent_parser import build_horizon_context
from tradingagents.agents.utils.agent_states import current_tracker_var, extract_verdict


def create_social_media_analyst(llm, data_collector=None):
    async def _safe(tool, payload):
        try:
            return await asyncio.to_thread(tool.invoke, payload)
        except Exception as exc:
            return f"调用失败：{exc}"

    async def social_media_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        horizon = "short"  # 情绪面固定短期视角
        user_intent = state.get("user_intent") or {}
        focus_areas = user_intent.get("focus_areas", [])
        specific_questions = user_intent.get("specific_questions", [])

        config = get_config()
        system_message = get_prompt("social_system_message", config=config)
        horizon_ctx = build_horizon_context(horizon, focus_areas, specific_questions, agent_type="social")

        pool = data_collector.get(ticker, current_date) if data_collector else None
        social_text = None

        if pool is not None:
            news_text = pool.get("news", "无数据")
            trending = pool.get("trending_tickers", "无数据")
        else:
            from datetime import datetime, timedelta
            from tradingagents.agents.utils.agent_utils import (
                get_news, get_trending_tickers, get_social_sentiment,
            )
            days = 7
            end_dt = datetime.strptime(current_date, "%Y-%m-%d")
            start_dt = end_dt - timedelta(days=days)

            # Parallelize fallback fetches (including social sentiment)
            results = await asyncio.gather(
                _safe(get_news, {
                    "ticker": ticker, "start_date": start_dt.strftime("%Y-%m-%d"), "end_date": current_date,
                }),
                _safe(get_trending_tickers, {"market": "HK" if ticker.endswith(".HK") else "US", "top_n": 20}),
                _safe(get_social_sentiment, {"symbol": ticker}),
            )
            news_text, trending, social_text = results

        # Fetch social sentiment even when pool is available (pool doesn't have it)
        if pool is not None:
            from tradingagents.agents.utils.agent_utils import get_social_sentiment
            social_text = await _safe(get_social_sentiment, {"symbol": ticker})
        social_section = (
            f"\n\n【社交舆情 (Reddit/X/Polymarket)】\n{social_text}"
            if social_text and "unavailable" not in str(social_text).lower()
            else ""
        )

        messages = [
            SystemMessage(content=(
                system_message
                + "\n\n请严格基于提供的舆情数据输出报告，全程使用中文。"
                + "\n如果提供了社交舆情数据（Reddit/X/Polymarket），请在分析中综合考量海外社交平台的情绪和提及量。"
            )),
            HumanMessage(content=(
                horizon_ctx + "\n"
                f"以下是 {ticker} 在 {current_date} 的舆情近似资料。\n\n"
                f"【get_news】\n{news_text}\n\n"
                f"【市场热门股票（按换手率）】\n{trending}" 
                f"{social_section}\n"
            )),
        ]

        # ── 实现 Token 级流式输出 ──────────────────
        tracker = current_tracker_var.get()
        full_content = ""
        async for chunk in llm.astream(messages):
            content = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += content
            if tracker:
                tracker._emit_token("Social Analyst", "sentiment_report", content)

        verdict, confidence = extract_verdict(full_content)

        # ── 数据来源追溯 ──────────────────
        from tradingagents.agents.utils.data_trace import build_data_trace, summarize_data
        trace = build_data_trace("社交舆情分析师", [
            ("get_news", "SearchService(4引擎)", summarize_data(news_text)),
            ("get_trending_tickers", "Futu", summarize_data(trending)),
            ("get_social_sentiment", "Adanos API", summarize_data(social_text)),
        ])
        full_content += trace

        return {
            "sentiment_report": full_content,
            "analyst_traces": [{
                "agent": "social_media_analyst",
                "horizon": horizon,
                "data_window": "7天",
                "key_finding": f"舆情分析结论：{verdict}",
                "verdict": verdict,
                "confidence": confidence,
            }],
        }

    return social_media_analyst_node
