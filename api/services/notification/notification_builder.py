"""Report message builder — renders ReportDB into plain/markdown/HTML text.

Used by scheduler and dispatch_notification to format report content
for different notification channels.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from api.database import ReportDB

logger = logging.getLogger(__name__)


class NotificationBuilder:
    """Render report data into channel-appropriate message formats.

    - **Plain** (telegram, custom webhook): plain text
    - **Markdown** (wechat, feishu, discord, slack): Markdown with tables
    - **Rich** (email): full HTML with inline CSS
    """

    _DIRECTION_ALIAS = {
        "BULLISH": "看多",
        "LEAN_BULLISH": "偏多",
        "BEARISH": "看空",
        "LEAN_BEARISH": "偏空",
        "NEUTRAL": "中性",
        "CAUTIOUS": "谨慎",
    }

    _DIRECTION_EMOJI = {
        "看多": "🟢",
        "偏多": "🟡",
        "多": "🟢",
        "看空": "🔴",
        "偏空": "🟠",
        "空": "🔴",
        "中性": "⚪",
        "谨慎": "⚠️",
    }

    # -- public API ---------------------------------------------------------

    @classmethod
    def build_plain(cls, report: "ReportDB", stock_name: str = "") -> str:
        """Build a plain-text message body (Telegram, custom webhook)."""
        lines = cls._header_lines(report, stock_name)
        lines += cls._verdict_lines(report)
        lines += cls._decision_lines(report)
        lines += cls._risk_lines(report)
        lines += cls._footer_lines()
        return "\n".join(lines)

    @classmethod
    def build_markdown(cls, report: "ReportDB", stock_name: str = "") -> str:
        """Build a Markdown message body (WeChat, Feishu, Discord, Slack)."""
        lines = cls._header_lines(report, stock_name)
        lines += cls._verdict_lines(report)
        lines += cls._decision_lines(report)
        lines += cls._risk_lines(report)
        lines += cls._footer_lines()
        km = getattr(report, "key_metrics", None)
        if km:
            lines.append("")
            lines.append("**关键指标**")
            lines.append("")
            lines.append("| 指标 | 数值 | 状态 |")
            lines.append("|------|------|------|")
            status_label = {"good": "✅良好", "neutral": "⚪中性", "bad": "❌不佳"}
            for item in km:
                name = item.get("name", "")
                value = item.get("value", "")
                status = status_label.get(item.get("status", ""), item.get("status", ""))
                lines.append(f"| {name} | {value} | {status} |")
        return "\n".join(lines)

    @classmethod
    def build_html(cls, report: "ReportDB", stock_name: str = "") -> str:
        """Build HTML body — delegates to email_report_service renderer."""
        from api.services.email_report_service import render_report_html
        return render_report_html(report, stock_name=stock_name)

    # -- internal helpers ---------------------------------------------------

    @classmethod
    def _header_lines(cls, report: "ReportDB", stock_name: str) -> List[str]:
        symbol = str(report.symbol or "")
        trade_date = str(report.trade_date or "")
        direction = str(report.direction or "")
        direction = cls._DIRECTION_ALIAS.get(direction.upper(), direction)
        emoji = cls._DIRECTION_EMOJI.get(direction, "")

        display = f"{stock_name} {symbol}" if stock_name and stock_name != symbol else symbol
        lines = [
            f"TradingAgents 投研报告",
            f"股票：{display}",
            f"交易日：{trade_date}",
        ]
        if direction:
            lines.append(f"方向：{emoji} {direction}")
        confidence = getattr(report, "confidence", None)
        if confidence is not None:
            lines.append(f"置信度：{confidence}%")
        return lines

    @classmethod
    def _verdict_lines(cls, report: "ReportDB") -> List[str]:
        """Extract agent verdicts from report fields."""
        import re, json
        verdict_re = re.compile(r"<!--\s*VERDICT:\s*(\{[^>]+\})\s*-->")
        agent_sections = [
            ("market_report", "市场分析"),
            ("sentiment_report", "舆情分析"),
            ("news_report", "新闻分析"),
            ("fundamentals_report", "基本面分析"),
            ("macro_report", "宏观分析"),
            ("smart_money_report", "主力资金分析"),
            ("volume_price_report", "量价分析"),
        ]
        verdicts: List[tuple[str, dict]] = []
        for attr, title in agent_sections:
            content = getattr(report, attr, None)
            if not content:
                continue
            m = verdict_re.search(content)
            if not m:
                continue
            try:
                parsed = json.loads(m.group(1))
                direction = parsed.get("direction", "")
                reason = parsed.get("reason", "").strip()[:42]
                if direction and reason:
                    direction = cls._DIRECTION_ALIAS.get(direction.upper(), direction)
                    verdicts.append((title, {"direction": direction, "reason": reason}))
            except (json.JSONDecodeError, AttributeError):
                continue

        if not verdicts:
            return []
        lines = ["", "**各方观点**"]
        for title, v in verdicts:
            emoji = cls._DIRECTION_EMOJI.get(v["direction"], "")
            lines.append(f"- {title}：{emoji} {v['direction']} — {v['reason']}")
        return lines

    @classmethod
    def _decision_lines(cls, report: "ReportDB") -> List[str]:
        lines = []
        decision = getattr(report, "decision", None)
        if decision:
            lines.append(f"决策：{decision}")
        target = getattr(report, "target_price", None)
        if target is not None:
            lines.append(f"目标价：¥{target:.2f}")
        stop_loss = getattr(report, "stop_loss_price", None)
        if stop_loss is not None:
            lines.append(f"止损价：¥{stop_loss:.2f}")
        return lines

    @classmethod
    def _risk_lines(cls, report: "ReportDB") -> List[str]:
        risk_items = getattr(report, "risk_items", None)
        if not risk_items:
            return []
        level_label = {"high": "高", "medium": "中", "low": "低"}
        lines = ["", "**风险提示**"]
        for item in risk_items:
            name = item.get("name", "")
            level = level_label.get(item.get("level", ""), item.get("level", ""))
            lines.append(f"- [{level}] {name}")
        return lines

    @classmethod
    def _footer_lines(cls) -> List[str]:
        return [
            "",
            "—— TradingAgents 投研系统",
        ]
