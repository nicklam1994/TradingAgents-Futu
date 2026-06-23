# -*- coding: utf-8 -*-
"""通知路由配置助手。

本模块故意只使用纯字符串，不导入 NotificationChannel 枚举，
以避免与运行时通知服务产生循环依赖。

路由类型：
- report:       股票日报、每日分析、市场回顾
- alert:        事件驱动的预警通知
- system_error: 系统错误通知
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 可路由的通知渠道（纯字符串，与 NotificationChannel 枚举值一一对应）
# ---------------------------------------------------------------------------

ROUTABLE_NOTIFICATION_CHANNELS: Tuple[str, ...] = (
    "wechat",
    "feishu",
    "telegram",
    "email",
    "pushover",
    "ntfy",
    "gotify",
    "pushplus",
    "serverchan3",
    "custom",
    "discord",
    "slack",
    "astrbot",
)

ROUTABLE_NOTIFICATION_CHANNEL_SET = frozenset(ROUTABLE_NOTIFICATION_CHANNELS)

# ---------------------------------------------------------------------------
# 路由配置表
# ---------------------------------------------------------------------------

NOTIFICATION_ROUTE_CONFIGS: Dict[str, Dict[str, str]] = {
    "report": {
        "env_key": "NOTIFICATION_REPORT_CHANNELS",
        "config_attr": "notification_report_channels",
        "description": "Routes stock, daily, and market-review report notifications.",
    },
    "alert": {
        "env_key": "NOTIFICATION_ALERT_CHANNELS",
        "config_attr": "notification_alert_channels",
        "description": "Routes event-driven alert notifications.",
    },
    "system_error": {
        "env_key": "NOTIFICATION_SYSTEM_ERROR_CHANNELS",
        "config_attr": "notification_system_error_channels",
        "description": "Routes future system error notifications.",
    },
}


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------

def parse_notification_route_channels(raw_value: object) -> List[str]:
    """解析逗号分隔的路由渠道字符串，保留所有 token（含无效的）。"""
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        items: Iterable[object] = raw_value.split(",")
    elif isinstance(raw_value, (list, tuple, set)):
        items = raw_value
    else:
        items = [raw_value]

    channels: List[str] = []
    for item in items:
        token = str(item).strip().lower()
        if token:
            channels.append(token)
    return channels


def split_notification_route_channels(
    channels: Iterable[object],
) -> Tuple[List[str], List[str]]:
    """返回 (有效渠道, 无效渠道) 的有序去重列表。"""
    valid: List[str] = []
    invalid: List[str] = []
    seen_valid: set[str] = set()
    seen_invalid: set[str] = set()

    for channel in parse_notification_route_channels(channels):
        if channel in ROUTABLE_NOTIFICATION_CHANNEL_SET:
            if channel not in seen_valid:
                valid.append(channel)
                seen_valid.add(channel)
        elif channel not in seen_invalid:
            invalid.append(channel)
            seen_invalid.add(channel)
    return valid, invalid


def get_notification_route_config(route_type: Optional[str]) -> Optional[Dict[str, str]]:
    """根据路由类型返回路由元数据，未知路由返回 None。"""
    if route_type is None:
        return None
    return NOTIFICATION_ROUTE_CONFIGS.get(str(route_type).strip().lower())
