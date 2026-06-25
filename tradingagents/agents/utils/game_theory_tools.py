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


def _to_futu(symbol: str) -> str:
    """Convert canonical symbol to Futu format (US.AAPL / HK.00700)."""
    from tradingagents.dataflows.stock_resolver import to_futu
    return to_futu(symbol)


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
        futu_code = _to_futu(symbol)
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
        futu_code = _to_futu(symbol)
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
        futu_code = _to_futu(symbol)
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_owner_plate(futu_code)
            if ret != 0:
                return f"Concept tags error: {data}"
            if data.empty:
                return f"No concept tags for {symbol}"

            name = data.iloc[0].get("name", symbol)
            plates = data["plate_name"].tolist()
            lines = [f"## {name} ({symbol}) - {len(plates)} concept tags\n"]
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

        futu_code = _to_futu(symbol)
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

        futu_code = _to_futu(symbol)
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


@tool
def get_financial_report(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
    report_type: Annotated[str, "'income' (P&L), 'balance' (balance sheet), or 'cashflow' (cash flow)"] = "income",
) -> str:
    """Get financial statements from Futu — income statement, balance sheet, or cashflow.

    Returns revenue, profit, costs, assets, liabilities, equity with YoY changes.
    Works for HK and US stocks (coverage varies by market cap).

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
        report_type: 'income' for P&L, 'balance' for balance sheet, 'cashflow' for cash flow
    """
    try:
        from futu import OpenQuoteContext
        import os

        futu_code = _to_futu(symbol)

        # financial_type: 5=H1, 7=FY (full year)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_financials_statements(futu_code)
            if ret != 0:
                return f"Financial report error: {data}"

            reports = data.get("report_list", [])
            if not reports:
                return f"No financial data for {symbol}"

            # Use the most recent report
            r = reports[0]
            date_str = r.get("date_time_str", "")
            period = r.get("period_text", "")
            currency = r.get("currency_code", "")
            items = r.get("item_list", [])

            # Field ID mapping
            INCOME_FIELDS = {
                5001: "Revenue", 5002: "Total Revenue",
                5005: "Net Profit", 5008: "Net Profit (Parent)",
                5010: "Operating Profit", 5013: "Total Cost",
                5015: "R&D Expense", 5016: "Selling Expense",
                5017: "Admin Expense", 5019: "Finance Expense",
            }
            BALANCE_FIELDS = {
                5032: "Total Assets", 5034: "Total Liabilities",
                5035: "Total Equity", 5036: "Minority Interest",
            }
            CASHFLOW_FIELDS = {
                5040: "Operating Cashflow", 5041: "Investing Cashflow",
                5043: "Financing Cashflow", 5045: "Free Cashflow",
                5046: "Net Cashflow",
            }

            if report_type == "balance":
                field_map = BALANCE_FIELDS
                title = "Balance Sheet"
            elif report_type == "cashflow":
                field_map = CASHFLOW_FIELDS
                title = "Cash Flow Statement"
            else:
                field_map = INCOME_FIELDS
                title = "Income Statement"

            lines = [f"## {symbol} {title} ({period}, {currency})\n"]
            lines.append("| Item | Amount | YoY Change |")
            lines.append("|------|--------|------------|")

            for item in items:
                fid = item.get("field_id")
                if fid not in field_map:
                    continue
                val = item.get("data", 0)
                yoy = item.get("yoy", 0)
                name = field_map[fid]
                # Format
                if abs(val) >= 1e9:
                    val_str = f"{val/1e9:+.2f}B"
                elif abs(val) >= 1e6:
                    val_str = f"{val/1e6:+.2f}M"
                elif abs(val) >= 1e3:
                    val_str = f"{val/1e3:+.0f}K"
                else:
                    val_str = f"{val:+.0f}"
                yoy_str = f"{yoy:+.1f}%" if yoy else "N/A"
                lines.append(f"| {name} | {val_str} | {yoy_str} |")

            # Also show other reports available
            if len(reports) > 1:
                lines.append(f"\n*Also available: {len(reports)} periods (FY/H1/Q1/Q3)*")

            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Financial report error: {e}"


@tool
def get_capital_distribution(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
) -> str:
    """Get capital distribution — super/big/mid/small order inflow vs outflow breakdown.

    Shows how much money flows in vs out for each order size category.
    Complements get_capital_flow with detailed in/out split.

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
    """
    try:
        from futu import OpenQuoteContext
        import os
        futu_code = _to_futu(symbol)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_capital_distribution(futu_code)
            if ret != 0:
                return f"Capital distribution error: {data}"
            if data.empty:
                return f"No capital distribution data for {symbol}"
            row = data.iloc[0]
            ts = str(row.get("update_time", ""))
            def fmt(v):
                if v and v != "N/A":
                    v = float(v)
                    if abs(v) >= 1e9: return f"{v/1e9:+.2f}B"
                    if abs(v) >= 1e6: return f"{v/1e6:+.2f}M"
                    return f"{v:+.0f}"
                return "N/A"
            lines = [f"## {symbol} Capital Distribution ({ts})\n"]
            lines.append("| Type | Inflow | Outflow | Net |")
            lines.append("|------|--------|---------|-----|")
            for label, in_key, out_key in [
                ("Super Large", "capital_in_super", "capital_out_super"),
                ("Large", "capital_in_big", "capital_out_big"),
                ("Medium", "capital_in_mid", "capital_out_mid"),
                ("Small", "capital_in_small", "capital_out_small"),
            ]:
                inp = float(row.get(in_key, 0) or 0)
                out = float(row.get(out_key, 0) or 0)
                net = inp - out
                lines.append(f"| {label} | {fmt(inp)} | {fmt(out)} | {fmt(net)} |")
            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Capital distribution error: {e}"


@tool
def get_revenue_breakdown(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
) -> str:
    """Get revenue breakdown by business segment and geographic region.

    Shows what percentage of revenue comes from each product line and region.
    Essential for understanding business composition and concentration risk.

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
    """
    try:
        from futu import OpenQuoteContext
        import os
        futu_code = _to_futu(symbol)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_financials_revenue_breakdown(futu_code)
            if ret != 0:
                return f"Revenue breakdown error: {data}"
            period = data.get("period", "")
            currency = data.get("currency_code", "")
            breakdown = data.get("breakdown_list", [])
            if not breakdown:
                return f"No revenue breakdown for {symbol}"
            TYPE_MAP = {1: "Business Segment", 4: "Geographic Region", 8: "Business (Detailed)"}
            lines = [f"## {symbol} Revenue Breakdown ({period}, {currency})\n"]
            for group in breakdown:
                gtype = group.get("type", 0)
                label = TYPE_MAP.get(gtype, f"Type {gtype}")
                items = group.get("item_list", [])
                if not items:
                    continue
                lines.append(f"### {label}")
                lines.append("| Segment | Revenue | Share |")
                lines.append("|---------|---------|-------|")
                for item in items:
                    name = item.get("name", "?")
                    rev = item.get("main_oper_income", 0)
                    ratio = item.get("ratio", 0)
                    if abs(rev) >= 1e9:
                        rev_str = f"{rev/1e9:.2f}B"
                    elif abs(rev) >= 1e6:
                        rev_str = f"{rev/1e6:.1f}M"
                    else:
                        rev_str = f"{rev:,.0f}"
                    lines.append(f"| {name} | {rev_str} | {ratio:.1f}% |")
                lines.append("")
            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Revenue breakdown error: {e}"


@tool
def get_institutional_holders(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
) -> str:
    """Get institutional holdings — how many institutions hold the stock and their share%.

    Shows quarterly institutional holder count, total shares held, and percentage changes.
    Useful for judging if smart money is accumulating or reducing positions.

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
    """
    try:
        from futu import OpenQuoteContext
        import os
        futu_code = _to_futu(symbol)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_shareholders_institutional(futu_code)
            if ret != 0:
                return f"Institutional holders error: {data}"
            if data.empty:
                return f"No institutional data for {symbol}"
            lines = [f"## {symbol} Institutional Holdings\n"]
            lines.append("| Period | Institutions | Shares Held | Share% | Change |")
            lines.append("|--------|-------------|-------------|--------|--------|")
            for _, row in data.head(8).iterrows():
                period = row.get("period_text", "")
                count = int(row.get("institution_quantity", 0))
                count_chg = int(row.get("institution_quantity_change", 0))
                shares = float(row.get("holder_quantity", 0))
                pct = float(row.get("holder_pct", 0))
                pct_chg = float(row.get("holder_pct_change", 0))
                if shares >= 1e9:
                    shares_str = f"{shares/1e9:.2f}B"
                elif shares >= 1e6:
                    shares_str = f"{shares/1e6:.1f}M"
                else:
                    shares_str = f"{shares:,.0f}"
                chg_str = f"{pct_chg:+.2f}%" if pct_chg else "N/A"
                lines.append(f"| {period} | {count} ({count_chg:+d}) | {shares_str} | {pct:.2f}% | {chg_str} |")
            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Institutional holders error: {e}"


@tool
def get_holder_changes(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
) -> str:
    """Get shareholder holding changes — who bought/sold recently and how much.

    Shows recent institutional and insider transactions with share counts and prices.
    Critical for detecting smart money movements before they show in price.

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
    """
    try:
        from futu import OpenQuoteContext
        import os
        futu_code = _to_futu(symbol)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_shareholders_holding_changes(futu_code)
            if ret != 0:
                return f"Holder changes error: {data}"
            if data.empty:
                return f"No holder change data for {symbol}"
            lines = [f"## {symbol} Recent Shareholder Changes\n"]
            lines.append("| Date | Holder | Type | Shares Changed | Price | Share% |")
            lines.append("|------|--------|------|---------------|-------|--------|")
            for _, row in data.head(10).iterrows():
                date = str(row.get("holding_date_str", ""))
                name = str(row.get("name", "?"))[:20]
                htype = str(row.get("holder_type", ""))
                chg = float(row.get("share_change_num", 0))
                price = float(row.get("shares_change_price", 0))
                ratio = float(row.get("share_ratio", 0))
                ratio_chg = float(row.get("share_ratio_change", 0))
                chg_str = f"{chg:+,.0f}" if chg else "N/A"
                price_str = f"{price:,.0f}" if price else "N/A"
                ratio_str = f"{ratio:.2f}% ({ratio_chg:+.2f}%)" if ratio else "N/A"
                lines.append(f"| {date} | {name} | {htype} | {chg_str} | {price_str} | {ratio_str} |")
            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Holder changes error: {e}"


@tool
def get_dividend_history(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
) -> str:
    """Get dividend history — payout dates, amounts, and yield.

    Shows historical dividend payments for income-focused analysis.
    Useful for evaluating dividend sustainability and yield attractiveness.

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
    """
    try:
        from futu import OpenQuoteContext
        import os
        futu_code = _to_futu(symbol)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_corporate_actions_dividends(futu_code)
            if ret != 0:
                return f"Dividend error: {data}"
            divs = data.get("dividend_list", [])
            if not divs:
                return f"No dividend history for {symbol}"
            lines = [f"## {symbol} Dividend History\n"]
            lines.append("| Ex-Date | Record Date | Pay Date | Amount | Type |")
            lines.append("|---------|-------------|----------|--------|------|")
            for d in divs[:10]:
                ex = d.get("ex_dividend_date", "")
                record = d.get("record_date", "")
                pay = d.get("payment_date", "")
                amount = d.get("dividend_amount", 0)
                dtype = d.get("dividend_type", "")
                lines.append(f"| {ex} | {record} | {pay} | {amount} | {dtype} |")
            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Dividend error: {e}"


@tool
def get_financial_alerts(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
) -> str:
    """Get financial unusual activity alerts — earnings surprises, restatements, etc.

    Flags unusual financial events that may impact stock price.
    Early warning system for fundamental risks.

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
    """
    try:
        from futu import OpenQuoteContext
        import os
        futu_code = _to_futu(symbol)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_financial_unusual(futu_code)
            if ret != 0:
                return f"Financial alerts error: {data}"
            content = data.get("content", [])
            if not content:
                return f"No financial unusual activity for {symbol}"
            lines = [f"## {symbol} Financial Alerts\n"]
            for item in content:
                lines.append(f"- {item}")
            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Financial alerts error: {e}"


@tool
def get_technical_alerts(
    symbol: Annotated[str, "Stock code, e.g. AAPL or 00020.HK"],
) -> str:
    """Get technical unusual activity alerts — breakout signals, volume spikes, etc.

    Flags unusual technical patterns that may indicate momentum shifts.
    Useful for timing entries/exits.

    Args:
        symbol: Stock code, e.g. AAPL or 00020.HK
    """
    try:
        from futu import OpenQuoteContext
        import os
        futu_code = _to_futu(symbol)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_technical_unusual(futu_code)
            if ret != 0:
                return f"Technical alerts error: {data}"
            content = data.get("content", [])
            if not content:
                return f"No technical unusual activity for {symbol}"
            lines = [f"## {symbol} Technical Alerts\n"]
            for item in content:
                lines.append(f"- {item}")
            return "\n".join(lines)
        finally:
            ctx.close()
    except Exception as e:
        return f"Technical alerts error: {e}"


def get_hk_social_sentiment(symbol: str) -> str:
    """Build synthetic social sentiment for HK stocks from Futu data.

    Fallback when Adanos API doesn't cover HK stocks. Combines:
    - Short interest (空仓数据)
    - Capital flow direction (资金流向)
    - Top broker activity (经纪商动向)
    """
    try:
        from futu import OpenQuoteContext, RET_OK
        import os

        futu_code = _to_futu(symbol)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            sections = []

            # 1. Short interest
            ret, _, data = ctx.get_short_interest(futu_code)
            if ret == RET_OK and data is not None and not data.empty:
                latest = data.iloc[0]
                shares = latest.get("shares_short", 0)
                pct = latest.get("short_percent", 0)
                days = latest.get("days_to_cover", 0)
                date_str = latest.get("timestamp_str", "N/A")
                sections.append(
                    f"【做空数据】(截至 {date_str})\n"
                    f"- 做空股数: {shares:,.0f}\n"
                    f"- 做空比例: {pct:.2f}%\n"
                    f"- 平仓天数: {days:.1f}天"
                )
                if pct > 5:
                    sections.append("  ⚠ 做空比例偏高，空头压力显著")
                elif pct > 2:
                    sections.append("  做空比例中等")
                else:
                    sections.append("  做空比例较低，空头压力有限")

            # 2. Capital flow direction
            ret2, flow_data = ctx.get_capital_flow(futu_code)
            if ret2 == RET_OK and flow_data is not None and not flow_data.empty:
                latest_flow = flow_data.iloc[-1]
                in_flow = latest_flow.get("in_flow", 0)
                if isinstance(in_flow, (int, float)):
                    direction = "净流入" if in_flow > 0 else "净流出"
                    sections.append(
                        f"【资金流向】\n"
                        f"- 总资金: {direction} {abs(in_flow)/1e8:.2f}亿"
                    )
                main_in = latest_flow.get("main_in_flow", 0)
                if isinstance(main_in, (int, float)):
                    direction = "净流入" if main_in > 0 else "净流出"
                    sections.append(
                        f"- 主力资金: {direction} {abs(main_in)/1e8:.2f}亿"
                    )

            # 3. Top broker activity
            ret3, broker_data = ctx.get_top_ten_buy_sell_brokers(futu_code)
            if ret3 == RET_OK and broker_data is not None and not broker_data.empty:
                buyers = broker_data[broker_data["buy_sell_type"] == 1].head(3)
                sellers = broker_data[broker_data["buy_sell_type"] == 2].head(3)
                if not buyers.empty:
                    top_buy = buyers.iloc[0]
                    sections.append(
                        f"【经纪商动向】\n"
                        f"- 头部买方: {top_buy.get('broker_name', 'N/A')} "
                        f"(净买 {top_buy.get('net_vol', 0)/1e4:.0f}万股)"
                    )
                if not sellers.empty:
                    top_sell = sellers.iloc[0]
                    sections.append(
                        f"- 头部卖方: {top_sell.get('broker_name', 'N/A')} "
                        f"(净卖 {abs(top_sell.get('net_vol', 0))/1e4:.0f}万股)"
                    )

            if not sections:
                return f"No HK social sentiment data available for {symbol}"

            return (
                f"## {symbol} 港股情绪指标（Futu 合成）\n\n"
                + "\n\n".join(sections)
                + "\n\n*注：港股无 Reddit/X 数据，以上为 Futu 做空+资金+经纪商合成指标*"
            )
        finally:
            ctx.close()
    except Exception as e:
        return f"HK social sentiment error: {e}"
