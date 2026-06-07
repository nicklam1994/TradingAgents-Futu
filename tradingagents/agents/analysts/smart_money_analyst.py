import asyncio

from langchain_core.messages import HumanMessage, SystemMessage
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt
from tradingagents.graph.intent_parser import build_horizon_context
from tradingagents.agents.utils.agent_states import current_tracker_var, extract_verdict


def _is_hk(ticker: str) -> bool:
    """Check if ticker is a HK stock."""
    return ticker.endswith(".HK") or ticker.startswith("HK.")


def create_smart_money_analyst(llm, data_collector=None):
    async def _safe(tool, payload):
        try:
            return await asyncio.to_thread(tool.invoke, payload)
        except Exception as exc:
            return f"调用失败：{exc}"

    async def smart_money_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        print(f"[Smart Money Analyst] START {ticker} {current_date}")
        horizon = "short"  # 资金面固定短期视角
        user_intent = state.get("user_intent") or {}
        focus_areas = user_intent.get("focus_areas", [])
        specific_questions = user_intent.get("specific_questions", [])

        config = get_config()
        system_message = get_prompt("smart_money_system_message", config=config) or ""
        horizon_ctx = build_horizon_context(horizon, focus_areas, specific_questions, agent_type="smart_money")

        pool = data_collector.get(ticker, current_date) if data_collector else None
        is_hk = _is_hk(ticker)

        # ── 按市场分层获取数据 ──────────────────
        trace_entries = []  # For data source trace
        if is_hk:
            # 港股：资金流向 + VWMA + 换手率排名
            if pool is not None:
                volume = pool.get("indicators", {}).get("vwma", "无数据")
                trending = pool.get("trending_tickers", "无数据")
            else:
                from tradingagents.agents.utils.agent_utils import get_indicators, get_trending_tickers
                volume, trending = await asyncio.gather(
                    _safe(get_indicators, {
                        "symbol": ticker, "indicator": "vwma",
                        "curr_date": current_date, "look_back_days": 20,
                    }),
                    _safe(get_trending_tickers, {"market": "HK", "top_n": 20}),
                )

            from tradingagents.agents.utils.agent_utils import (
                get_capital_flow, get_capital_distribution, get_top_ten_broker,
                get_institutional_holders, get_holder_changes, get_analyst_consensus,
            )
            capital_flow, capital_dist, broker_data, inst_holders, holder_changes, consensus = await asyncio.gather(
                _safe(get_capital_flow, {"symbol": ticker}),
                _safe(get_capital_distribution, {"symbol": ticker}),
                _safe(get_top_ten_broker, {"symbol": ticker}),
                _safe(get_institutional_holders, {"symbol": ticker}),
                _safe(get_holder_changes, {"symbol": ticker}),
                _safe(get_analyst_consensus, {"symbol": ticker}),
            )

            data_section = (
                f"【港股资金流向（超大单/大单/中单/小单净流入）】\n{capital_flow}\n\n"
                f"【资金进出明细（超大/大/中/小单 in/out）】\n{capital_dist}\n\n"
                f"【十大经纪商买卖排行】\n{broker_data}\n\n"
                f"【机构持仓（季度变化）】\n{inst_holders}\n\n"
                f"【股东增减持明细】\n{holder_changes}\n\n"
                f"【分析师共识评级】\n{consensus}\n\n"
                f"【成交量加权均价 VWMA】\n{volume}\n\n"
                f"【港股热门股票（按换手率）】\n{trending}"
            )
            from tradingagents.agents.utils.data_trace import summarize_data
            trace_entries = [
                ("get_capital_flow", "Futu", summarize_data(capital_flow)),
                ("get_capital_distribution", "Futu", summarize_data(capital_dist)),
                ("get_top_ten_broker", "Futu", summarize_data(broker_data)),
                ("get_institutional_holders", "Futu", summarize_data(inst_holders)),
                ("get_holder_changes", "Futu", summarize_data(holder_changes)),
                ("get_analyst_consensus", "Futu", summarize_data(consensus)),
                ("get_indicators(vwma)", "Futu+stockstats", summarize_data(volume)),
                ("get_trending_tickers", "Futu", summarize_data(trending)),
            ]
        else:
            # 美股：成交量异动 + VWMA + 社交情绪 + 换手率 + Morningstar + 分析师评级
            if pool is not None:
                volume = pool.get("indicators", {}).get("vwma", "无数据")
                trending = pool.get("trending_tickers", "无数据")
            else:
                from tradingagents.agents.utils.agent_utils import get_indicators, get_trending_tickers
                volume, trending = await asyncio.gather(
                    _safe(get_indicators, {
                        "symbol": ticker, "indicator": "vwma",
                        "curr_date": current_date, "look_back_days": 20,
                    }),
                    _safe(get_trending_tickers, {"market": "US", "top_n": 20}),
                )

            from tradingagents.agents.utils.agent_utils import get_social_sentiment, get_morningstar_report, get_analyst_consensus
            sentiment, morningstar, consensus = await asyncio.gather(
                _safe(get_social_sentiment, {"symbol": ticker}),
                _safe(get_morningstar_report, {"symbol": ticker}),
                _safe(get_analyst_consensus, {"symbol": ticker}),
            )

            data_section = (
                f"【Morningstar 研究报告】\n{morningstar}\n\n"
                f"【分析师共识评级】\n{consensus}\n\n"
                f"【成交量加权均价 VWMA】\n{volume}\n\n"
                f"【社交舆情（Reddit/X/Polymarket）】\n{sentiment}\n\n"
                f"【美股热门股票（按换手率）】\n{trending}"
            )
            from tradingagents.agents.utils.data_trace import summarize_data
            trace_entries = [
                ("get_morningstar_report", "Futu", summarize_data(morningstar)),
                ("get_analyst_consensus", "Futu", summarize_data(consensus)),
                ("get_indicators(vwma)", "Futu+stockstats", summarize_data(volume)),
                ("get_social_sentiment", "Adanos API", summarize_data(sentiment)),
                ("get_trending_tickers", "Futu", summarize_data(trending)),
            ]

        # ── 构建提示词 ──────────────────
        market_label = "港股" if is_hk else "美股"
        guidance = ""
        if is_hk:
            guidance = (
                "\n\n港股分析指引：\n"
                "1. 请结合 VWMA（成交量加权移动平均线）与股价的相对位置进行判断：若股价在 VWMA 上方且 VWMA 呈上升趋势，说明资金承接意愿强；反之则说明抛压较重。\n"
                "2. 请额外关注港股特有的'南向资金'（港股通）动向，作为主力资金的重要替代指标。\n"
                "3. 港股没有涨跌停限制，需特别注意异常放量和换手率变化。"
            )
        else:
            guidance = (
                "\n\n美股分析指引：\n"
                "1. 美股没有实时'主力资金'数据，请从成交量异动、VWMA 趋势、社交舆情三个维度综合判断资金方向。\n"
                "2. 关注 Reddit/X 上的散户情绪极值（过度看多可能是反向信号）。\n"
                "3. 关注 Dark Pool 活动和 Options Flow（如有数据）作为机构动向的间接指标。"
            )

        messages = [
            SystemMessage(content=(
                system_message
                + f"\n\n你正在分析{market_label}市场。请使用{market_label}市场的分析框架。"
                + "\n\n请严格基于提供的量化数据输出分析，全程使用中文。"
                + guidance
            )),
            HumanMessage(content=(
                horizon_ctx + "\n"
                f"请分析 {ticker} 在 {current_date} 的资金行为与成交量特征。\n\n"
                f"市场类型：{market_label}\n\n"
                + data_section
            )),
        ]

        # ── Token 级流式输出 ──────────────────
        tracker = current_tracker_var.get()
        full_content = ""
        async for chunk in llm.astream(messages):
            content = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += content
            if tracker:
                tracker._emit_token("Smart Money Analyst", "smart_money_report", content)

        verdict, confidence = extract_verdict(full_content)
        print(f"[Smart Money Analyst] DONE {ticker} ({market_label}), report length={len(full_content)}")

        # ── 数据来源追溯 ──────────────────
        from tradingagents.agents.utils.data_trace import build_data_trace
        trace = build_data_trace("机构资金行为分析师", trace_entries)
        full_content += trace

        return {
            "smart_money_report": full_content,
            "analyst_traces": [{
                "agent": "smart_money_analyst",
                "horizon": horizon,
                "data_window": "近期可用",
                "key_finding": f"主力资金分析结论：{verdict}",
                "verdict": verdict,
                "confidence": confidence,
            }],
        }

    return smart_money_analyst_node
