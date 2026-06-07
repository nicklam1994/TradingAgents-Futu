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
