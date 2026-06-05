"""Notification channel abstraction, routing service, and message builder.

This module provides:
- ``NotificationChannel`` enum for the 7 supported delivery channels.
- ``NotificationSender`` ABC that every concrete sender implements.
- ``NotificationService`` that dispatches messages to the right channel(s)
  based on route type and user configuration.
- ``NotificationBuilder`` that renders report data into Markdown/HTML bodies
  suitable for each channel's formatting capabilities.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from api.database import ReportDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel enum
# ---------------------------------------------------------------------------

class NotificationChannel(str, Enum):
    """Supported notification delivery channels."""

    EMAIL = "email"
    WECHAT = "wechat"          # 企业微信 webhook
    FEISHU = "feishu"          # 飞书 webhook
    TELEGRAM = "telegram"      # Telegram Bot API
    DISCORD = "discord"        # Discord webhook
    SLACK = "slack"            # Slack webhook
    CUSTOM_WEBHOOK = "custom_webhook"  # 通用 webhook (含钉钉等)


# ---------------------------------------------------------------------------
# Abstract base sender
# ---------------------------------------------------------------------------

class NotificationSender(ABC):
    """Abstract base class for all notification senders.

    Each concrete sender must implement ``send(message, config) -> bool``.
    The *config* dict carries channel-specific credentials/settings
    (API keys, webhook URLs, chat IDs, etc.).
    """

    channel: NotificationChannel

    @abstractmethod
    def send(self, message: str, config: Dict[str, Any]) -> bool:
        """Send *message* using *config*. Return True on success."""

    async def send_async(self, message: str, config: Dict[str, Any]) -> bool:
        """Async wrapper — defaults to running ``send`` in a thread."""
        import asyncio
        return await asyncio.to_thread(self.send, message, config)


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

class NotificationBuilder:
    """Render report data into channel-appropriate message formats.

    Channels are split into two formatting tiers:
    - **Rich** (email): full HTML with inline CSS.
    - **Markdown** (wechat, feishu, discord, slack): Markdown with tables.
    - **Plain** (telegram, custom_webhook): plain text, no HTML/Markdown
      tables (Telegram supports some Markdown but is fragile).
    """

    # -- direction / color helpers ------------------------------------------

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
        # Markdown tables for key metrics
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
        """Build HTML body — delegates to the existing email_report_service renderer."""
        from api.services.email_report_service import render_report_html
        return render_report_html(report, stock_name=stock_name)

    # -- internal helpers ---------------------------------------------------

    @classmethod
    def _header_lines(cls, report: "ReportDB", stock_name: str) -> List[str]:
        symbol = report.symbol or ""
        trade_date = report.trade_date or ""
        direction = report.direction or ""
        direction = cls._DIRECTION_ALIAS.get(direction.upper(), direction)
        emoji = cls._DIRECTION_EMOJI.get(direction, "")

        display = f"{stock_name} {symbol}" if stock_name and stock_name != symbol else symbol
        lines = [
            f"TradingAgents 投研报告",
            f"标的：{display}",
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
            desc = item.get("description", "")
            lines.append(f"- [{level}] {name}：{desc}")
        return lines

    @classmethod
    def _footer_lines(cls) -> List[str]:
        return [
            "",
            "---",
            "本报告由 TradingAgents 多智能体系统自动生成，仅供参考，不构成投资建议。",
        ]


# ---------------------------------------------------------------------------
# Notification service — dispatches to channels
# ---------------------------------------------------------------------------

class NotificationService:
    """Central notification dispatcher.

    Usage::

        service = NotificationService()
        service.register(NotificationChannel.TELEGRAM, telegram_sender)
        await service.dispatch(report, route_type="REPORT", user_config={...})
    """

    def __init__(self) -> None:
        self._senders: Dict[NotificationChannel, NotificationSender] = {}

    def register(self, channel: NotificationChannel, sender: NotificationSender) -> None:
        """Register a sender for *channel*."""
        self._senders[channel] = sender
        logger.info("[notification] registered sender for %s", channel.value)

    def get_sender(self, channel: NotificationChannel) -> Optional[NotificationSender]:
        return self._senders.get(channel)

    async def dispatch(
        self,
        report: "ReportDB",
        route_type: str,
        user_config: Dict[str, Any],
        stock_name: str = "",
    ) -> Dict[str, bool]:
        """Dispatch a report notification to all enabled channels for *route_type*.

        *user_config* is the full notification config dict from DB, shaped like::

            {
                "email": {"enabled": true, "address": "..."},
                "wechat": {"enabled": true, "webhook_url": "..."},
                ...
            }

        Returns a dict mapping channel name -> success bool.
        """
        from api.services.notification.notification_routing import (
            get_channels_for_route,
        )
        from api.services.notification.notification_noise import (
            should_send,
        )

        channels = get_channels_for_route(route_type, user_config)
        results: Dict[str, bool] = {}

        for channel in channels:
            sender = self._senders.get(channel)
            if sender is None:
                logger.warning("[notification] no sender registered for %s", channel.value)
                results[channel.value] = False
                continue

            ch_config = user_config.get(channel.value, {})
            if not ch_config.get("enabled", False):
                results[channel.value] = False
                continue

            # Noise check: skip if a duplicate was sent recently
            symbol = report.symbol or ""
            if not should_send(symbol, route_type, channel.value):
                logger.info("[noise] suppressed %s for %s/%s", channel.value, symbol, route_type)
                results[channel.value] = False
                continue

            # Build message body appropriate for the channel
            message = self._build_message(report, channel, stock_name)

            try:
                ok = await sender.send_async(message, ch_config)
                results[channel.value] = ok
                if ok:
                    from api.services.notification.notification_noise import record_sent
                    record_sent(symbol, route_type, channel.value)
            except Exception as exc:
                logger.error("[notification] %s send failed: %s", channel.value, exc)
                results[channel.value] = False

        return results

    @staticmethod
    def _build_message(report: "ReportDB", channel: NotificationChannel, stock_name: str) -> str:
        """Choose the right message format for *channel*."""
        if channel == NotificationChannel.EMAIL:
            # Email uses full HTML — the sender handles rendering
            return NotificationBuilder.build_html(report, stock_name)

        if channel in (
            NotificationChannel.WECHAT,
            NotificationChannel.FEISHU,
            NotificationChannel.DISCORD,
            NotificationChannel.SLACK,
        ):
            return NotificationBuilder.build_markdown(report, stock_name)

        # Telegram and custom_webhook get plain text
        return NotificationBuilder.build_plain(report, stock_name)


# Module-level singleton — importers can use ``notification_service`` directly
notification_service = NotificationService()
