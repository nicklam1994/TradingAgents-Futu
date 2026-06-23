# -*- coding: utf-8 -*-
"""
通知配置诊断

职责：
1. 检查渠道配置完整性（minimal key / advanced key）
2. 检查路由配置有效性
3. 检查噪音控制配置
4. 输出结构化诊断结果（errors / warnings / info）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from tradingagents.notification.core import (
    ChannelDetector,
    NotificationChannel,
    NotificationService,
)
from tradingagents.notification.noise import (
    NOTIFICATION_SEVERITIES,
    P4_NOISE_ENV_KEYS,
    is_supported_notification_severity,
    parse_notification_quiet_hours,
    validate_notification_timezone,
)
from tradingagents.notification.routing import (
    NOTIFICATION_ROUTE_CONFIGS,
    ROUTABLE_NOTIFICATION_CHANNELS,
    split_notification_route_channels,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

KeyTier = Literal["minimal", "advanced"]
IssueSeverity = Literal["error", "warning", "info"]
ChannelKind = Literal["configured", "fallback", "context"]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NotificationKeySpec:
    """通知配置 key 的元数据。"""
    key: str
    tier: KeyTier
    description: str
    channel: str


@dataclass(frozen=True)
class NotificationChannelSpec:
    """通知渠道基线元数据。"""
    channel: str
    display_name: str
    kind: ChannelKind
    minimal_keys: Tuple[str, ...]
    alternative_minimal_keys: Tuple[Tuple[str, ...], ...] = ()
    advanced_keys: Tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class NotificationDiagnosticIssue:
    """一条诊断消息。"""
    severity: IssueSeverity
    code: str
    message: str
    key: Optional[str] = None


@dataclass(frozen=True)
class NotificationDiagnosticResult:
    """结构化通知诊断结果。"""
    configured_channels: Tuple[str, ...]
    errors: Tuple[NotificationDiagnosticIssue, ...]
    warnings: Tuple[NotificationDiagnosticIssue, ...]
    info: Tuple[NotificationDiagnosticIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# 渠道规格表
# ---------------------------------------------------------------------------

CHANNEL_SPECS: Tuple[NotificationChannelSpec, ...] = (
    NotificationChannelSpec(
        channel=NotificationChannel.WECHAT.value,
        display_name=ChannelDetector.get_channel_name(NotificationChannel.WECHAT),
        kind="configured",
        minimal_keys=("WECHAT_WEBHOOK_URL",),
        advanced_keys=("WECHAT_MSG_TYPE",),
    ),
    NotificationChannelSpec(
        channel=NotificationChannel.FEISHU.value,
        display_name=ChannelDetector.get_channel_name(NotificationChannel.FEISHU),
        kind="configured",
        minimal_keys=("FEISHU_WEBHOOK_URL",),
        advanced_keys=("FEISHU_WEBHOOK_SECRET", "FEISHU_WEBHOOK_KEYWORD"),
    ),
    NotificationChannelSpec(
        channel=NotificationChannel.TELEGRAM.value,
        display_name=ChannelDetector.get_channel_name(NotificationChannel.TELEGRAM),
        kind="configured",
        minimal_keys=("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
        advanced_keys=("TELEGRAM_MESSAGE_THREAD_ID",),
    ),
    NotificationChannelSpec(
        channel=NotificationChannel.EMAIL.value,
        display_name=ChannelDetector.get_channel_name(NotificationChannel.EMAIL),
        kind="configured",
        minimal_keys=("EMAIL_SENDER", "EMAIL_PASSWORD"),
        advanced_keys=("EMAIL_RECEIVERS", "EMAIL_SENDER_NAME"),
    ),
    NotificationChannelSpec(
        channel=NotificationChannel.DISCORD.value,
        display_name=ChannelDetector.get_channel_name(NotificationChannel.DISCORD),
        kind="configured",
        minimal_keys=("DISCORD_WEBHOOK_URL",),
        alternative_minimal_keys=(("DISCORD_BOT_TOKEN", "DISCORD_MAIN_CHANNEL_ID"),),
        note="Webhook URL or bot token + channel ID can enable Discord.",
    ),
    NotificationChannelSpec(
        channel=NotificationChannel.SLACK.value,
        display_name=ChannelDetector.get_channel_name(NotificationChannel.SLACK),
        kind="configured",
        minimal_keys=("SLACK_WEBHOOK_URL",),
        alternative_minimal_keys=(("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID"),),
        note="Webhook URL or bot token + channel ID can enable Slack.",
    ),
)

# P0 需要额外检查的 key
P0_ACTIONS_ENV_KEYS: Tuple[str, ...] = (
    "FEISHU_WEBHOOK_SECRET",
    "FEISHU_WEBHOOK_KEYWORD",
)


# ---------------------------------------------------------------------------
# 诊断引擎
# ---------------------------------------------------------------------------

def _issue(
    severity: IssueSeverity,
    code: str,
    message: str,
    key: Optional[str] = None,
) -> NotificationDiagnosticIssue:
    return NotificationDiagnosticIssue(severity=severity, code=code, message=message, key=key)


def _has(config: Dict[str, Any], key: str) -> bool:
    """检查配置中是否有非空值（大小写不敏感 key 匹配）。"""
    # 先尝试原始 key
    value = config.get(key)
    if value is not None:
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        return str(value).strip() != ""
    # 尝试小写 key
    value = config.get(key.lower())
    if value is not None:
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        return str(value).strip() != ""
    return False


def _require_pair(
    config: Dict[str, Any],
    *,
    left_key: str,
    right_key: str,
    channel_name: str,
    errors: List[NotificationDiagnosticIssue],
    warnings: Optional[List[NotificationDiagnosticIssue]] = None,
    severity: IssueSeverity = "error",
) -> None:
    """检查配置 key 对是否完整。"""
    left = _has(config, left_key)
    right = _has(config, right_key)
    target = errors if severity == "error" else (warnings if warnings is not None else errors)
    if left and not right:
        target.append(
            _issue(severity, "partial_channel_config",
                   f"{channel_name} 已配置 {left_key}，但缺少 {right_key}，该渠道不会启用。",
                   key=right_key)
        )
    if right and not left:
        target.append(
            _issue(severity, "partial_channel_config",
                   f"{channel_name} 已配置 {right_key}，但缺少 {left_key}，该渠道不会启用。",
                   key=left_key)
        )


def run_notification_diagnostics(config: Dict[str, Any]) -> NotificationDiagnosticResult:
    """运行通知配置只读诊断。

    Args:
        config: 配置字典（键为大写环境变量名或小写属性名）。

    Returns:
        NotificationDiagnosticResult
    """
    # 检测已配置渠道
    configured = tuple(
        channel.value
        for channel in NotificationService.detect_configured_channels(config)
    )
    errors: List[NotificationDiagnosticIssue] = []
    warnings: List[NotificationDiagnosticIssue] = []
    info: List[NotificationDiagnosticIssue] = [
        _issue("info", "phase_scope",
               "通知诊断会检查渠道基线、路由配置和噪音控制配置。"),
    ]

    if not configured:
        errors.append(
            _issue("error", "no_channels_configured",
                   "0 个通知渠道已配置；如需发送通知，请至少配置一个渠道的 minimal key。")
        )

    # 渠道 key 对检查
    _require_pair(config, left_key="TELEGRAM_BOT_TOKEN", right_key="TELEGRAM_CHAT_ID",
                  channel_name="Telegram", errors=errors)
    _require_pair(config, left_key="EMAIL_SENDER", right_key="EMAIL_PASSWORD",
                  channel_name="邮件", errors=errors)
    _require_pair(config, left_key="DISCORD_BOT_TOKEN", right_key="DISCORD_MAIN_CHANNEL_ID",
                  channel_name="Discord Bot", errors=errors, warnings=warnings,
                  severity="warning" if _has(config, "DISCORD_WEBHOOK_URL") else "error")
    _require_pair(config, left_key="SLACK_BOT_TOKEN", right_key="SLACK_CHANNEL_ID",
                  channel_name="Slack Bot", errors=errors, warnings=warnings,
                  severity="warning" if _has(config, "SLACK_WEBHOOK_URL") else "error")

    # Advanced key 没有 minimal key 的警告
    if (_has(config, "FEISHU_WEBHOOK_SECRET") or _has(config, "FEISHU_WEBHOOK_KEYWORD")) \
            and not _has(config, "FEISHU_WEBHOOK_URL"):
        warnings.append(
            _issue("warning", "advanced_without_minimal",
                   "已配置飞书 Webhook 高级安全项，但缺少 FEISHU_WEBHOOK_URL，飞书渠道不会启用。",
                   key="FEISHU_WEBHOOK_URL")
        )

    # 路由配置检查
    configured_set = set(configured)
    for route_type, route_config in NOTIFICATION_ROUTE_CONFIGS.items():
        config_attr = route_config["config_attr"]
        env_key = route_config["env_key"]
        # 支持大写和小写 key
        route_channels = config.get(env_key) or config.get(config_attr) or []
        if not route_channels:
            continue

        valid_channels, invalid_channels = split_notification_route_channels(route_channels)
        if invalid_channels:
            errors.append(
                _issue("error", "invalid_route_channel",
                       f"{env_key} 包含未知通知渠道: {', '.join(invalid_channels)}；"
                       f"允许值: {', '.join(ROUTABLE_NOTIFICATION_CHANNELS)}。",
                       key=env_key)
            )

        disabled_channels = [ch for ch in valid_channels if ch not in configured_set]
        if disabled_channels:
            warnings.append(
                _issue("warning", "route_channel_not_configured",
                       f"{env_key} 路由 {route_type} 指向未启用渠道: "
                       f"{', '.join(disabled_channels)}；这些渠道不会收到该类型通知。",
                       key=env_key)
            )

    # 噪音控制配置检查
    quiet_hours_raw = config.get("NOTIFICATION_QUIET_HOURS") or config.get("notification_quiet_hours") or ""
    if quiet_hours_raw:
        try:
            parse_notification_quiet_hours(str(quiet_hours_raw))
        except ValueError as exc:
            errors.append(
                _issue("error", "invalid_quiet_hours",
                       f"NOTIFICATION_QUIET_HOURS 配置无效: {exc}",
                       key="NOTIFICATION_QUIET_HOURS")
            )

    timezone_raw = config.get("NOTIFICATION_TIMEZONE") or config.get("notification_timezone") or ""
    if timezone_raw:
        try:
            validate_notification_timezone(str(timezone_raw))
        except ValueError as exc:
            errors.append(
                _issue("error", "invalid_notification_timezone",
                       f"NOTIFICATION_TIMEZONE 配置无效: {exc}",
                       key="NOTIFICATION_TIMEZONE")
            )

    min_severity_raw = str(
        config.get("NOTIFICATION_MIN_SEVERITY") or config.get("notification_min_severity") or ""
    ).strip().lower()
    if min_severity_raw and not is_supported_notification_severity(min_severity_raw):
        errors.append(
            _issue("error", "invalid_notification_min_severity",
                   f"NOTIFICATION_MIN_SEVERITY 配置无效；允许值: {', '.join(NOTIFICATION_SEVERITIES)}。",
                   key="NOTIFICATION_MIN_SEVERITY")
        )

    return NotificationDiagnosticResult(
        configured_channels=configured,
        errors=tuple(errors),
        warnings=tuple(warnings),
        info=tuple(info),
    )


def format_notification_diagnostics(result: NotificationDiagnosticResult) -> str:
    """将诊断结果格式化为人类可读文本。"""

    def _format_issues(title: str, issues: Sequence[NotificationDiagnosticIssue]) -> List[str]:
        if not issues:
            return [f"{title}: 无"]
        lines = [f"{title}:"]
        for item in issues:
            key_suffix = f" [{item.key}]" if item.key else ""
            lines.append(f"  - {item.code}{key_suffix}: {item.message}")
        return lines

    lines = [
        "通知配置诊断",
        f"已配置渠道: {len(result.configured_channels)} 个",
    ]
    if result.configured_channels:
        channel_names = [
            ChannelDetector.get_channel_name(NotificationChannel(ch))
            for ch in result.configured_channels
        ]
        lines.append("渠道列表: " + ", ".join(channel_names))
    else:
        lines.append("渠道列表: (无)")

    lines.append("")
    lines.extend(_format_issues("Errors", result.errors))
    lines.append("")
    lines.extend(_format_issues("Warnings", result.warnings))
    lines.append("")
    lines.extend(_format_issues("Info", result.info))
    return "\n".join(lines)
