# -*- coding: utf-8 -*-
"""
===================================
TradingAgents-Futu - 通知层
===================================

职责：
1. 多渠道消息推送（企业微信、飞书、Telegram、邮件、Discord、Slack 等）
2. 消息路由（report / alert / system_error 三种路由类型）
3. 噪音控制（去重、冷却、静默时段、最低级别过滤）
4. Markdown/HTML 消息构建
"""

from tradingagents.notification.core import (
    ChannelAttemptResult,
    ChannelDetector,
    NotificationBuilder,
    NotificationChannel,
    NotificationDispatchResult,
    NotificationService,
    Sender,
)
from tradingagents.notification.routing import (
    NOTIFICATION_ROUTE_CONFIGS,
    ROUTABLE_NOTIFICATION_CHANNELS,
    get_notification_route_config,
    parse_notification_route_channels,
    split_notification_route_channels,
)
from tradingagents.notification.noise import (
    NotificationNoiseDecision,
    evaluate_notification_noise,
    record_notification_noise,
    release_notification_noise,
    reset_notification_noise_state,
)

__all__ = [
    # Core
    "NotificationChannel",
    "Sender",
    "NotificationService",
    "NotificationBuilder",
    "ChannelDetector",
    "ChannelAttemptResult",
    "NotificationDispatchResult",
    # Routing
    "NOTIFICATION_ROUTE_CONFIGS",
    "ROUTABLE_NOTIFICATION_CHANNELS",
    "get_notification_route_config",
    "parse_notification_route_channels",
    "split_notification_route_channels",
    # Noise
    "NotificationNoiseDecision",
    "evaluate_notification_noise",
    "record_notification_noise",
    "release_notification_noise",
    "reset_notification_noise_state",
]
