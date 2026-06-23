# -*- coding: utf-8 -*-
"""
通知核心框架

职责：
1. NotificationChannel 枚举 —— 定义所有支持的渠道类型
2. Sender ABC —— 通知发送器抽象基类
3. NotificationBuilder —— Markdown/HTML 消息构建器
4. ChannelDetector —— 渠道检测器
5. NotificationService —— 通知服务（注册 sender、路由分发、噪音控制）
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from tradingagents.notification.routing import (
    get_notification_route_config,
    split_notification_route_channels,
)
from tradingagents.notification.noise import (
    NotificationNoiseDecision,
    evaluate_notification_noise,
    record_notification_noise,
    release_notification_noise,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# N1.2: NotificationChannel enum + Sender ABC
# ---------------------------------------------------------------------------

class NotificationChannel(Enum):
    """通知渠道类型"""

    WECHAT = "wechat"          # 企业微信
    FEISHU = "feishu"          # 飞书
    TELEGRAM = "telegram"      # Telegram
    EMAIL = "email"            # 邮件
    PUSHOVER = "pushover"      # Pushover（手机/桌面推送）
    NTFY = "ntfy"              # ntfy
    GOTIFY = "gotify"          # Gotify
    PUSHPLUS = "pushplus"      # PushPlus（国内推送服务）
    SERVERCHAN3 = "serverchan3"  # Server酱3
    CUSTOM = "custom"          # 自定义 Webhook
    DISCORD = "discord"        # Discord 机器人
    SLACK = "slack"            # Slack
    ASTRBOT = "astrbot"        # AstrBot
    UNKNOWN = "unknown"        # 未知


class Sender(ABC):
    """通知发送器抽象基类。

    所有渠道发送器必须继承此类并实现 ``send`` 方法。
    ``__init__`` 接收配置字典，从中读取渠道所需的环境变量/配置项。
    """

    # 子类必须声明对应的渠道类型
    channel: NotificationChannel = NotificationChannel.UNKNOWN

    def __init__(self, config: Dict[str, Any]) -> None:
        """初始化发送器，从 config 字典读取渠道配置。"""

    @abstractmethod
    def send(self, content: str, *, title: Optional[str] = None,
             timeout_seconds: Optional[float] = None,
             image_bytes: Optional[bytes] = None) -> bool:
        """发送消息。

        Args:
            content: 消息正文（Markdown 或纯文本，视渠道而定）。
            title: 可选标题。
            timeout_seconds: 发送超时。
            image_bytes: 可选 PNG 图片字节（用于不支持 Markdown 的渠道）。

        Returns:
            True 表示发送成功，False 表示失败。
        """

    def is_configured(self) -> bool:
        """检查该渠道是否已配置（至少具备最小必需 key）。"""
        return False


# ---------------------------------------------------------------------------
# N1.2: ChannelAttemptResult / NotificationDispatchResult
# ---------------------------------------------------------------------------

@dataclass
class ChannelAttemptResult:
    """单次渠道发送尝试结果。"""

    channel: str
    success: bool
    error_code: Optional[str] = None
    retryable: bool = False
    latency_ms: Optional[int] = None
    diagnostics: Optional[str] = None


@dataclass
class NotificationDispatchResult:
    """通知分发结构化结果。"""

    dispatched: bool
    success: bool
    status: str
    channel_results: List[ChannelAttemptResult] = field(default_factory=list)
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# N1.2: ChannelDetector
# ---------------------------------------------------------------------------

_CHANNEL_NAMES: Dict[NotificationChannel, str] = {
    NotificationChannel.WECHAT: "企业微信",
    NotificationChannel.FEISHU: "飞书",
    NotificationChannel.TELEGRAM: "Telegram",
    NotificationChannel.EMAIL: "邮件",
    NotificationChannel.PUSHOVER: "Pushover",
    NotificationChannel.NTFY: "ntfy",
    NotificationChannel.GOTIFY: "Gotify",
    NotificationChannel.PUSHPLUS: "PushPlus",
    NotificationChannel.SERVERCHAN3: "Server酱3",
    NotificationChannel.CUSTOM: "自定义Webhook",
    NotificationChannel.DISCORD: "Discord机器人",
    NotificationChannel.SLACK: "Slack",
    NotificationChannel.ASTRBOT: "ASTRBOT机器人",
    NotificationChannel.UNKNOWN: "未知渠道",
}


class ChannelDetector:
    """渠道检测器 —— 根据配置判断可用渠道。"""

    @staticmethod
    def get_channel_name(channel: NotificationChannel) -> str:
        """获取渠道中文名称。"""
        return _CHANNEL_NAMES.get(channel, "未知渠道")


# ---------------------------------------------------------------------------
# N1.3: NotificationBuilder (Markdown / HTML)
# ---------------------------------------------------------------------------

class NotificationBuilder:
    """消息构建器 —— 生成 Markdown 和 HTML 格式的通知消息。

    使用示例::

        builder = NotificationBuilder()
        builder.add_title("每日分析报告")
        builder.add_line("**股票代码**: 600519")
        builder.add_line("**信号**: 看多")
        md = builder.to_markdown()
        html = builder.to_html()
    """

    def __init__(self) -> None:
        self._lines: List[str] = []

    def add_title(self, title: str, level: int = 1) -> "NotificationBuilder":
        """添加标题行。"""
        prefix = "#" * min(max(level, 1), 6)
        self._lines.append(f"{prefix} {title}")
        self._lines.append("")
        return self

    def add_line(self, line: str = "") -> "NotificationBuilder":
        """添加一行文本。"""
        self._lines.append(line)
        return self

    def add_lines(self, lines: List[str]) -> "NotificationBuilder":
        """批量添加多行。"""
        self._lines.extend(lines)
        return self

    def add_separator(self) -> "NotificationBuilder":
        """添加分隔线。"""
        self._lines.append("---")
        self._lines.append("")
        return self

    def add_table(self, headers: List[str], rows: List[List[str]]) -> "NotificationBuilder":
        """添加 Markdown 表格。"""
        if not headers:
            return self
        # Header row
        self._lines.append("| " + " | ".join(headers) + " |")
        self._lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for row in rows:
            # Pad row to match header count
            padded = row + [""] * (len(headers) - len(row))
            self._lines.append("| " + " | ".join(padded[:len(headers)]) + " |")
        self._lines.append("")
        return self

    def to_markdown(self) -> str:
        """输出 Markdown 文本。"""
        return "\n".join(self._lines).strip()

    def to_html(self) -> str:
        """将 Markdown 转换为简单 HTML。

        注意：这是一个基础实现，仅处理常见格式。
        如需更精确的转换，可集成 markdown2 或 mistune。
        """
        import re
        md = self.to_markdown()
        html_lines: List[str] = []
        for line in md.split("\n"):
            # Headings
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading_match:
                level = len(heading_match.group(1))
                text = heading_match.group(2)
                html_lines.append(f"<h{level}>{text}</h{level}>")
                continue
            # Horizontal rule
            if line.strip() in ("---", "***", "___"):
                html_lines.append("<hr>")
                continue
            # Bold
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            # Italic
            line = re.sub(r"\*(.+?)\*", r"<em>\1</em>", line)
            # Inline code
            line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
            if line.strip():
                html_lines.append(f"<p>{line}</p>")
            else:
                html_lines.append("")
        return "\n".join(html_lines)

    def clear(self) -> "NotificationBuilder":
        """清空已构建的内容。"""
        self._lines.clear()
        return self

    def __len__(self) -> int:
        return len(self._lines)


# ---------------------------------------------------------------------------
# N1.2: NotificationService
# ---------------------------------------------------------------------------

class NotificationService:
    """通知服务 —— 注册 sender、检测渠道、路由分发、噪音控制。

    使用示例::

        service = NotificationService()
        service.register_sender(EmailSender(config))
        service.register_sender(TelegramSender(config))
        result = service.send("分析完成", route_type="report")
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config: Dict[str, Any] = config or {}
        self._senders: Dict[NotificationChannel, Sender] = {}
        self._available_channels: List[NotificationChannel] = []

    # --- Sender 注册 ---

    def register_sender(self, sender: Sender) -> None:
        """注册一个渠道发送器。"""
        self._senders[sender.channel] = sender
        self._refresh_available_channels()
        logger.info("已注册通知渠道: %s", ChannelDetector.get_channel_name(sender.channel))

    def unregister_sender(self, channel: NotificationChannel) -> None:
        """注销一个渠道发送器。"""
        self._senders.pop(channel, None)
        self._refresh_available_channels()

    def _refresh_available_channels(self) -> None:
        """刷新已配置渠道列表。"""
        self._available_channels = [
            ch for ch, sender in self._senders.items()
            if sender.is_configured()
        ]
        if not self._available_channels:
            logger.warning("未配置有效的通知渠道，将不发送推送通知")
        else:
            names = [ChannelDetector.get_channel_name(ch) for ch in self._available_channels]
            logger.info("已配置 %d 个通知渠道: %s", len(names), ", ".join(names))

    # --- 渠道检测 ---

    @staticmethod
    def detect_configured_channels(config: Dict[str, Any]) -> List[NotificationChannel]:
        """根据配置字典静态检测已配置的渠道（不依赖 sender 注册）。

        支持大小写不敏感的 key 匹配。
        """
        # 构建小写 key -> value 映射以支持大小写不敏感查找
        lower_config = {k.lower(): v for k, v in config.items()}

        detected: List[NotificationChannel] = []
        channel_checks: Dict[NotificationChannel, List[str]] = {
            NotificationChannel.WECHAT: ["wechat_webhook_url"],
            NotificationChannel.FEISHU: ["feishu_webhook_url"],
            NotificationChannel.TELEGRAM: ["telegram_bot_token", "telegram_chat_id"],
            NotificationChannel.EMAIL: ["email_sender", "email_password"],
            NotificationChannel.PUSHOVER: ["pushover_user_key", "pushover_api_token"],
            NotificationChannel.NTFY: ["ntfy_url"],
            NotificationChannel.GOTIFY: ["gotify_url", "gotify_token"],
            NotificationChannel.PUSHPLUS: ["pushplus_token"],
            NotificationChannel.SERVERCHAN3: ["serverchan3_sendkey"],
            NotificationChannel.CUSTOM: ["custom_webhook_urls"],
            NotificationChannel.DISCORD: ["discord_webhook_url"],
            NotificationChannel.SLACK: ["slack_webhook_url"],
            NotificationChannel.ASTRBOT: ["astrbot_url"],
        }
        for channel, keys in channel_checks.items():
            if all(lower_config.get(k) for k in keys):
                detected.append(channel)
        return detected

    @property
    def available_channels(self) -> List[NotificationChannel]:
        """当前已配置且可用的渠道列表。"""
        return list(self._available_channels)

    # --- 路由解析 ---

    def _resolve_target_channels(
        self,
        route_type: Optional[str],
        target_channels: Optional[List[str]],
    ) -> List[NotificationChannel]:
        """解析目标渠道列表。

        优先使用显式指定的 target_channels；
        否则根据 route_type 从配置中读取路由渠道。
        """
        if target_channels:
            # 显式指定渠道名 -> 枚举映射
            name_to_enum = {ch.value: ch for ch in NotificationChannel}
            return [name_to_enum[name] for name in target_channels
                    if name in name_to_enum]

        if route_type:
            route_config = get_notification_route_config(route_type)
            if route_config:
                config_attr = route_config["config_attr"]
                route_channels = self._config.get(config_attr, [])
                valid, _invalid = split_notification_route_channels(route_channels)
                name_to_enum = {ch.value: ch for ch in NotificationChannel}
                return [name_to_enum[name] for name in valid
                        if name in name_to_enum]

        # 默认：所有已配置渠道
        return list(self._available_channels)

    # --- 发送 ---

    def send(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        route_type: Optional[str] = None,
        target_channels: Optional[List[str]] = None,
        severity: Optional[str] = None,
        dedup_key: Optional[str] = None,
        cooldown_key: Optional[str] = None,
        skip_noise_check: bool = False,
    ) -> NotificationDispatchResult:
        """发送通知。

        Args:
            content: 消息正文。
            title: 可选标题。
            route_type: 路由类型（report / alert / system_error）。
            target_channels: 显式指定目标渠道名列表（覆盖路由配置）。
            severity: 消息级别（info / warning / error / critical）。
            dedup_key: 去重 key（可选）。
            cooldown_key: 冷却 key（可选）。
            skip_noise_check: 跳过噪音检查。

        Returns:
            NotificationDispatchResult
        """
        # 1. 噪音控制
        noise_blocked_result: NotificationDispatchResult | None = None
        noise_decision: NotificationNoiseDecision | None = None
        if not skip_noise_check:
            decision = evaluate_notification_noise(
                _dict_to_config(self._config),
                content=content,
                route_type=route_type,
                severity=severity,
                dedup_key=dedup_key,
                cooldown_key=cooldown_key,
            )
            noise_decision = decision
            if not decision.should_send:
                logger.info("通知被噪音控制拦截: %s", decision.message)
                noise_blocked_result = NotificationDispatchResult(
                    dispatched=False,
                    success=False,
                    status="noise_blocked",
                    message=decision.message,
                )

        if noise_blocked_result is not None:
            return noise_blocked_result

        # 2. 解析目标渠道
        channels = self._resolve_target_channels(route_type, target_channels)
        if not channels:
            msg = "无可用的通知目标渠道"
            logger.warning(msg)
            if noise_decision:
                release_notification_noise(noise_decision)
            return NotificationDispatchResult(
                dispatched=False,
                success=False,
                status="no_channels",
                message=msg,
            )

        # 3. 逐渠道发送
        channel_results: List[ChannelAttemptResult] = []
        any_success = False
        import time as _time

        for channel in channels:
            sender = self._senders.get(channel)
            if sender is None:
                channel_results.append(ChannelAttemptResult(
                    channel=channel.value,
                    success=False,
                    error_code="sender_not_registered",
                    diagnostics="Sender 未注册",
                ))
                continue

            start = _time.monotonic()
            try:
                ok = sender.send(content, title=title)
                elapsed_ms = int((_time.monotonic() - start) * 1000)
                channel_results.append(ChannelAttemptResult(
                    channel=channel.value,
                    success=ok,
                    latency_ms=elapsed_ms,
                ))
                if ok:
                    any_success = True
            except Exception as exc:
                elapsed_ms = int((_time.monotonic() - start) * 1000)
                logger.error("渠道 %s 发送失败: %s", channel.value, exc)
                channel_results.append(ChannelAttemptResult(
                    channel=channel.value,
                    success=False,
                    error_code="send_exception",
                    latency_ms=elapsed_ms,
                    diagnostics=str(exc),
                ))

        # 4. 记录噪音状态 / 释放预留
        if noise_decision:
            if any_success:
                record_notification_noise(noise_decision)
            else:
                release_notification_noise(noise_decision)

        status = "success" if any_success else "all_failed"
        return NotificationDispatchResult(
            dispatched=True,
            success=any_success,
            status=status,
            channel_results=channel_results,
        )


def _dict_to_config(d: Dict[str, Any]):
    """将字典转为具有属性访问能力的对象，供 noise 模块使用。"""

    class _Config:
        pass

    cfg = _Config()
    for k, v in d.items():
        setattr(cfg, k, v)
    return cfg
