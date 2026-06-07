"""Market sentiment tools — US/HK replacements for A-share legacy tools.

Replaced:
  - get_board_fund_flow → get_sector_performance (Futu plate snapshot)
  - get_hot_stocks_xq → get_trending_tickers (Futu plate stocks by turnover)

Removed (no US/HK equivalent):
  - get_individual_fund_flow (主力资金 — concept doesn't exist)
  - get_lhb_detail (龙虎榜 — concept doesn't exist)
  - get_zt_pool (涨停池 — no daily limits in US/HK)
"""

from langchain_core.tools import tool
from typing import Annotated


@tool
def get_sector_performance(
    market: Annotated[str, "市场: US 或 HK"] = "US",
    top_n: Annotated[int, "返回前 N 个板块"] = 15,
) -> str:
    """获取行业板块行情排名，用于判断板块轮动信号。

    返回板块名称、涨跌幅、换手率，按涨跌幅排序。
    适用于 US（美股）和 HK（港股）市场。

    Args:
        market: 市场代码，US 或 HK
        top_n: 返回前 N 个板块（默认 15）
    """
    try:
        from futu import OpenQuoteContext, SysConfig, Plate
        import os
        SysConfig.enable_proto_encrypt(False)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, plates = ctx.get_plate_list(market=market, plate_class=Plate.INDUSTRY)
            if ret != 0:
                return f"获取板块列表失败: {plates}"

            plate_codes = plates["code"].tolist()
            ret, snapshot = ctx.get_market_snapshot(plate_codes[:50])
            if ret != 0:
                return f"获取板块行情失败: {snapshot}"

            snapshot = snapshot.sort_values("price_spread", ascending=False).head(top_n)

            lines = [f"## {market} 行业板块行情排名（前 {top_n}）\n"]
            lines.append("| 排名 | 板块 | 涨跌幅 | 换手率 |")
            lines.append("|------|------|--------|--------|")
            for i, (_, row) in enumerate(snapshot.iterrows(), 1):
                name = row.get("name", "")
                change_pct = row.get("price_spread", 0) or 0
                turnover = row.get("turnover_rate", 0) or 0
                lines.append(f"| {i} | {name} | {change_pct:+.2f}% | {turnover:.2f}% |")

            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"获取板块行情失败: {e}"


@tool
def get_trending_tickers(
    market: Annotated[str, "市场: US 或 HK"] = "US",
    top_n: Annotated[int, "返回前 N 只股票"] = 20,
) -> str:
    """获取当前市场热门股票（按换手率排序），反映市场关注热点。

    通过 Futu 行业板块成分股 + 实时行情，按换手率排名。
    适用于 US（美股）和 HK（港股）市场。

    Args:
        market: 市场代码，US 或 HK
        top_n: 返回前 N 只股票（默认 20）
    """
    try:
        from futu import OpenQuoteContext, SysConfig, Plate
        import os
        SysConfig.enable_proto_encrypt(False)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, plates = ctx.get_plate_list(market=market, plate_class=Plate.INDUSTRY)
            if ret != 0:
                return f"获取板块列表失败: {plates}"

            # Pick sectors with priority keywords
            priority_keywords = {
                "US": ["半导体", "互联网", "软件", "医疗", "银行", "消费", "汽车", "能源", "AI", "云"],
                "HK": ["科技", "医药", "银行", "消费", "汽车", "地产", "保险", "半导体"],
            }
            keywords = priority_keywords.get(market, priority_keywords["US"])
            selected = []
            for _, p in plates.iterrows():
                if any(kw in p["plate_name"] for kw in keywords):
                    selected.append(p["code"])
                if len(selected) >= 5:
                    break
            if not selected:
                selected = plates["code"].tolist()[:3]

            # Get stocks from selected plates
            all_stocks = []
            seen = set()
            for pc in selected:
                ret, stocks = ctx.get_plate_stock(pc)
                if ret == 0:
                    for _, s in stocks.iterrows():
                        if s["code"] not in seen:
                            seen.add(s["code"])
                            all_stocks.append(s["code"])

            if not all_stocks:
                return "未找到热门股票"

            # Get snapshot and sort by turnover
            ret, snapshot = ctx.get_market_snapshot(all_stocks[:100])
            if ret != 0:
                return f"获取行情失败: {snapshot}"

            snapshot = snapshot.sort_values("turnover_rate", ascending=False).head(top_n)

            lines = [f"## {market} 热门股票（按换手率，前 {top_n}）\n"]
            lines.append("| 排名 | 代码 | 名称 | 现价 | 涨跌幅 | 换手率 | 成交额 |")
            lines.append("|------|------|------|------|--------|--------|--------|")
            for i, (_, row) in enumerate(snapshot.iterrows(), 1):
                code = row.get("code", "")
                name = row.get("name", "")
                price = row.get("last_price", 0) or 0
                change = row.get("price_spread", 0) or 0
                turnover = row.get("turnover_rate", 0) or 0
                amount = row.get("turnover", 0) or 0
                amt_str = f"{amount/1e9:.1f}B" if amount >= 1e9 else (f"{amount/1e6:.1f}M" if amount >= 1e6 else f"{amount:.0f}")
                lines.append(f"| {i} | {code} | {name} | {price:.2f} | {change:+.2f}% | {turnover:.2f}% | {amt_str} |")

            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"获取热门股票失败: {e}"



@tool
def get_capital_flow(
    symbol: Annotated[str, "Stock code, e.g. US.AAPL or HK.00700"],
) -> str:
    """Get capital flow data for a stock - analyze institutional money direction.

    Returns net inflow/outflow by order size (super large/large/medium/small).
    Use this to judge if smart money is buying or selling.
    Works for US and HK markets.

    Args:
        symbol: Stock code, e.g. US.AAPL or HK.00700
    """
    try:
        from futu import OpenQuoteContext, SysConfig
        import os
        SysConfig.enable_proto_encrypt(False)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        # Convert canonical (00020.HK) to Futu format (HK.00020)
        futu_code = symbol
        if symbol.endswith(".HK"):
            futu_code = f"HK.{symbol[:-3]}"
        elif symbol.endswith(".US"):
            futu_code = f"US.{symbol[:-3]}"
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_capital_flow(futu_code)
            if ret != 0:
                return f"Capital flow error: {data}"
            if data.empty:
                return f"No capital flow data for {symbol}"

            latest = data.iloc[-1]

            def fmt(val):
                if val is None or str(val) == "N/A":
                    return "N/A"
                v = float(val)
                if abs(v) >= 1e9:
                    return f"{v/1e9:+.2f}B"
                elif abs(v) >= 1e6:
                    return f"{v/1e6:+.2f}M"
                return f"{v:+.0f}"

            in_flow = latest.get("in_flow", 0)
            super_in = latest.get("super_in_flow", 0)
            big_in = latest.get("big_in_flow", 0)
            mid_in = latest.get("mid_in_flow", 0)
            sml_in = latest.get("sml_in_flow", 0)
            main_in = latest.get("main_in_flow", "N/A")
            ts = latest.get("capital_flow_item_time", "")

            lines = [
                f"## {symbol} Capital Flow ({ts})\n",
                "| Type | Net Inflow |",
                "|------|-----------|",
                f"| Total | {fmt(in_flow)} |",
                f"| Super Large | {fmt(super_in)} |",
                f"| Large | {fmt(big_in)} |",
                f"| Medium | {fmt(mid_in)} |",
                f"| Small | {fmt(sml_in)} |",
            ]
            if main_in and str(main_in) != "N/A":
                lines.append(f"| Main Force | {fmt(main_in)} |")

            total = float(in_flow) if in_flow and str(in_flow) != "N/A" else 0
            lines.append(f"\nBullish" if total > 0 else "\nBearish")
            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Capital flow error: {e}"


@tool
def get_top_ten_broker(
    symbol: Annotated[str, "HK stock code, e.g. 00020.HK or 00700.HK"],
) -> str:
    """Get top 10 buy/sell brokers for a HK stock — identify institutional players.

    Shows which brokers are net buying vs selling, with volume and average price.
    Useful for identifying if foreign institutions (Goldman, UBS, JP Morgan) or
    mainland connect channels (沪港通/深港通) are the main force.
    Only works for HK stocks.

    Args:
        symbol: HK stock code, e.g. 00020.HK or 00700.HK
    """
    try:
        from futu import OpenQuoteContext
        import os

        # Convert canonical (00020.HK) to Futu format (HK.00020)
        futu_code = symbol
        if symbol.endswith(".HK"):
            futu_code = f"HK.{symbol[:-3]}"
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_top_ten_buy_sell_brokers(futu_code)
            if ret != 0:
                return f"Broker data error: {data}"
            if data.empty:
                return f"No broker data for {symbol}"

            buyers = data[data["buy_sell_type"] == 1].head(10)
            sellers = data[data["buy_sell_type"] == 2].head(10)

            lines = [f"## {symbol} Top 10 Buy/Sell Brokers\n"]

            lines.append("### Net Buyers (Top 10)")
            lines.append("| Broker | Net Vol | Avg Price | Total Vol |")
            lines.append("|--------|---------|-----------|-----------|")
            for _, row in buyers.iterrows():
                name = row.get("broker_name", "?")
                net = row.get("net_vol", 0)
                avg = row.get("avg_price", 0)
                total = row.get("total_vol", 0)
                lines.append(f"| {name} | {net:,.0f} | {avg:.3f} | {total:,.0f} |")

            lines.append("\n### Net Sellers (Top 10)")
            lines.append("| Broker | Net Vol | Avg Price | Total Vol |")
            lines.append("|--------|---------|-----------|-----------|")
            for _, row in sellers.iterrows():
                name = row.get("broker_name", "?")
                net = row.get("net_vol", 0)
                avg = row.get("avg_price", 0)
                total = row.get("total_vol", 0)
                lines.append(f"| {name} | {net:,.0f} | {avg:.3f} | {total:,.0f} |")

            # Highlight connect channels
            connect_brokers = [b for b in buyers["broker_name"].tolist() if "港" in b or "沪" in b or "深" in b]
            if connect_brokers:
                lines.append(f"\n⚠️ 港股通经纪商在买入方: {', '.join(connect_brokers)}")

            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Broker data error: {e}"


@tool
def get_stock_concept_tags(
    symbol: Annotated[str, "Stock code, e.g. US.AAPL or HK.00700"],
) -> str:
    """Get concept/theme tags for a stock - understand thematic classification.

    Returns all concept plates the stock belongs to (AI, 5G, Consumer Electronics, etc).
    Works for US and HK markets.

    Args:
        symbol: Stock code, e.g. US.AAPL or HK.00700
    """
    try:
        from futu import OpenQuoteContext, SysConfig
        import os
        SysConfig.enable_proto_encrypt(False)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        # Convert canonical (00020.HK) to Futu format (HK.00020)
        futu_code = symbol
        if symbol.endswith(".HK"):
            futu_code = f"HK.{symbol[:-3]}"
        elif symbol.endswith(".US"):
            futu_code = f"US.{symbol[:-3]}"
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_owner_plate(futu_code)
            if ret != 0:
                return f"Concept tags error: {data}"
            if data.empty:
                return f"No concept tags for {symbol}"

            name = data.iloc[0].get("name", symbol)
            plates = data["plate_name"].tolist()
            f"## {name} ({symbol}) - {len(plates)} concept tags\n" 
            for p in plates:
                lines.append(f"- {p}")
            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Concept tags error: {e}"


@tool
def screen_stocks(
    market: Annotated[str, "Market to screen: 'HK' or 'US'"],
    metric: Annotated[str, "Metric to filter: 'market_cap', 'pe_ratio', 'pb_ratio', 'turnover_rate', 'change_rate', 'volume'"] = "market_cap",
    min_val: Annotated[float, "Minimum value for the metric (0 = no limit)"] = 0,
    max_val: Annotated[float, "Maximum value for the metric (0 = no limit)"] = 0,
    limit: Annotated[int, "Max number of results to return"] = 20,
) -> str:
    """Screen stocks by financial metrics using Futu stock filter.

    Use this to find stocks matching specific criteria (e.g. high market cap, low PE).
    Useful for stock selection in autonomous trading loops.

    Supported metrics:
      - market_cap: Market capitalization in HKD/USD
      - pe_ratio: Price-to-Earnings ratio
      - pb_ratio: Price-to-Book ratio
      - turnover_rate: Trading turnover rate (%)
      - change_rate: Price change rate (%)
      - volume: Trading volume

    Args:
        market: 'HK' or 'US'
        metric: Which metric to filter on
        min_val: Minimum value (0 = no lower limit)
        max_val: Maximum value (0 = no upper limit)
        limit: Max results to return (default 20)
    """
    try:
        from futu import OpenQuoteContext, SimpleFilter, StockField, Market, RET_OK
        import os

        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)

        MARKET_MAP = {"HK": Market.HK, "US": Market.US}
        METRIC_MAP = {
            "market_cap": StockField.MARKET_VAL,
            "pe_ratio": StockField.PE_TTM,
            "pb_ratio": StockField.PB_RATE,
            "turnover_rate": StockField.TURNOVER_RATE,
            "change_rate": StockField.CHANGE_RATE,
            "volume": StockField.VOLUME,
        }

        mkt = MARKET_MAP.get(market.upper())
        if not mkt:
            return f"Invalid market: {market}. Use 'HK' or 'US'."

        field = METRIC_MAP.get(metric.lower())
        if not field:
            return f"Invalid metric: {metric}. Supported: {', '.join(METRIC_MAP.keys())}"

        try:
            sf = SimpleFilter()
            sf.stock_field = field
            sf.filter_min = min_val if min_val else -1e18
            sf.filter_max = max_val if max_val else 1e18
            sf.is_no_filter = False

            ret, result = ctx.get_stock_filter(
                market=mkt,
                filter_list=[sf],
                begin=0,
                num=min(limit, 100),
            )

            if ret != RET_OK:
                return f"Stock screen error: {result}"

            has_more, total, items = result
            if not items:
                return f"No stocks found matching criteria: {market} {metric} [{min_val}, {max_val}]"

            # Futu returns attribute name matching the filter field (lowercase)
            attr_name = field.lower() if isinstance(field, str) else str(field).lower()
            lines = [
                f"## Stock Screen: {market} — {metric} [{min_val or 'any'}, {max_val or 'any'}]",
                f"Showing {len(items)} of {total} results{' (more available)' if has_more else ''}\n",
                "| Code | Name | Value |",
                "|------|------|-------|",
            ]
            for item in items:
                code = item.stock_code.replace("HK.", "").replace("US.", "")
                if mkt == Market.HK:
                    code = f"{code}.HK"
                val = getattr(item, attr_name, None) or getattr(item, 'market_val', 'N/A')
                if isinstance(val, (int, float)):
                    lines.append(f"| {code} | {item.stock_name} | {val:,.2f} |")
                else:
                    lines.append(f"| {code} | {item.stock_name} | {val} |")

            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Stock screen error: {e}"


@tool
def get_morningstar_report(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
) -> str:
    """Get Morningstar research report for a stock — fair value, moat, valuation.

    Returns star rating (1-5), fair value estimate, economic moat assessment,
    valuation analysis, and bull/bear arguments.
    Works for both US and HK markets.

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
    """
    try:
        from futu import OpenQuoteContext
        import os

        futu_code = symbol
        if symbol.endswith(".HK"):
            futu_code = f"HK.{symbol[:-3]}"
        elif not symbol.startswith("US."):
            futu_code = f"US.{symbol}"
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_research_morningstar_report(futu_code)
            if ret != 0:
                return f"Morningstar error: {data}"
            if not data:
                return f"No Morningstar data for {symbol}"

            star = data.get("star_rating", "N/A")
            fair_value = data.get("fair_value", "N/A")
            moat = data.get("economic_moat_content", {}).get("context", "")
            valuation = data.get("valuation_content", {}).get("context", "")
            update_time = data.get("star_update_time_str", "")

            lines = [f"## {symbol} Morningstar Report ({update_time})\n"]
            lines.append(f"**Star Rating**: {'★' * star}{'☆' * (5 - star)} ({star}/5)")
            if isinstance(fair_value, (int, float)):
                lines.append(f"**Fair Value Estimate**: {fair_value:.2f}")
            if moat:
                lines.append(f"\n**Economic Moat**: {moat}")
            if valuation:
                lines.append(f"\n**Valuation Analysis**: {valuation[:500]}")

            # Bull/Bear
            bull = data.get("bull_say", [])
            bear = data.get("bear_say", [])
            if bull:
                lines.append("\n**Bull Case**:")
                for b in bull[:3]:
                    lines.append(f"- {b}")
            if bear:
                lines.append("\n**Bear Case**:")
                for b in bear[:3]:
                    lines.append(f"- {b}")

            pdf_url = data.get("pdf_url", "")
            if pdf_url:
                lines.append(f"\n[Full Report PDF]({pdf_url})")

            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Morningstar error: {e}"


@tool
def get_analyst_consensus(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
) -> str:
    """Get analyst consensus ratings — buy/hold/sell distribution and target prices.

    Shows how many analysts rate the stock as strong buy, buy, hold, sell,
    plus the highest/average/lowest target price.
    Works for both US and HK markets.

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
    """
    try:
        from futu import OpenQuoteContext
        import os

        futu_code = symbol
        if symbol.endswith(".HK"):
            futu_code = f"HK.{symbol[:-3]}"
        elif not symbol.startswith("US."):
            futu_code = f"US.{symbol}"
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_research_analyst_consensus(futu_code)
            if ret != 0:
                return f"Analyst consensus error: {data}"
            if not data:
                return f"No analyst consensus for {symbol}"

            total = data.get("total", 0)
            strong_buy = data.get("strong_buy", 0)
            buy = data.get("buy", 0)
            hold = data.get("hold", 0)
            sell = data.get("sell", 0)
            underperform = data.get("underperform", 0)
            highest = data.get("highest", 0)
            average = data.get("average", 0)
            lowest = data.get("lowest", 0)
            update_time = data.get("update_time_str", "")

            lines = [f"## {symbol} Analyst Consensus ({update_time})\n"]
            lines.append(f"**Total Analysts**: {total}")
            lines.append(f"**Rating**: {'★' * round(data.get('rating', 0))} ({data.get('rating', 0)}/5)\n")

            lines.append("| Rating | Percentage |")
            lines.append("|--------|------------|")
            lines.append(f"| Strong Buy | {strong_buy:.1f}% |")
            lines.append(f"| Buy | {buy:.1f}% |")
            lines.append(f"| Hold | {hold:.1f}% |")
            lines.append(f"| Sell | {sell:.1f}% |")
            lines.append(f"| Underperform | {underperform:.1f}% |")

            if any([highest, average, lowest]):
                lines.append(f"\n**Target Price**:")
                lines.append(f"- Highest: {highest:.2f}")
                lines.append(f"- Average: {average:.2f}")
                lines.append(f"- Lowest: {lowest:.2f}")

            # Sentiment summary
            bullish_pct = strong_buy + buy
            bearish_pct = sell + underperform
            if bullish_pct > 70:
                lines.append(f"\n🟢 **Strong Bullish Consensus** ({bullish_pct:.0f}% buy/strong-buy)")
            elif bullish_pct > 50:
                lines.append(f"\n🟡 **Moderately Bullish** ({bullish_pct:.0f}% buy/strong-buy)")
            elif bearish_pct > 50:
                lines.append(f"\n🔴 **Bearish Consensus** ({bearish_pct:.0f}% sell/underperform)")
            else:
                lines.append(f"\n⚪ **Mixed/Neutral Consensus**")

            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Analyst consensus error: {e}"
