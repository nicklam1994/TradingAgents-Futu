# -*- coding: utf-8 -*-
"""
通知系统桥接层 —— 连接 tradingagents.notification 核心框架与 API 层。

职责：
1. 从 DB + 环境变量构建通知配置字典
2. 创建 NotificationService 实例并注册 Sender
3. 提供 config 读写、测试发送、诊断的 API 级操作
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from tradingagents.notification.core import (
    ChannelDetector,
    NotificationChannel,
    NotificationService,
    Sender,
)
from tradingagents.notification.diagnostics import (
    NotificationDiagnosticResult,
    format_notification_diagnostics,
    run_notification_diagnostics,
)
from tradingagents.notification.routing import (
    NOTIFICATION_ROUTE_CONFIGS,
    ROUTABLE_NOTIFICATION_CHANNELS,
    get_notification_route_config,
    split_notification_route_channels,
)
from tradingagents.notification.alert_service import AlertService, AlertRule, AlertStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 单例 AlertService
# ---------------------------------------------------------------------------

_alert_service: Optional[AlertService] = None


def get_alert_service() -> AlertService:
    """获取全局 AlertService 单例。"""
    global _alert_service
    if _alert_service is None:
        _alert_service = AlertService()
    return _alert_service


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------

# 通知渠道所需的配置 key（大写环境变量名 -> 小写 config key）
_CHANNEL_CONFIG_KEYS: Dict[str, List[str]] = {
    "wechat": ["WECHAT_WEBHOOK_URL"],
    "feishu": ["FEISHU_WEBHOOK_URL"],
    "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
    "email": ["EMAIL_SENDER", "EMAIL_PASSWORD"],
    "discord": ["DISCORD_WEBHOOK_URL"],
    "slack": ["SLACK_WEBHOOK_URL"],
    "pushover": ["PUSHOVER_USER_KEY", "PUSHOVER_API_TOKEN"],
    "ntfy": ["NTFY_URL"],
    "gotify": ["GOTIFY_URL", "GOTIFY_TOKEN"],
    "pushplus": ["PUSHPLUS_TOKEN"],
    "serverchan3": ["SERVERCHAN3_SENDKEY"],
    "custom": ["CUSTOM_WEBHOOK_URLS"],
    "astrbot": ["ASTRBOT_URL"],
}

# 高级配置 key（可选）
_CHANNEL_ADVANCED_KEYS: Dict[str, List[str]] = {
    "feishu": ["FEISHU_WEBHOOK_SECRET", "FEISHU_WEBHOOK_KEYWORD"],
    "telegram": ["TELEGRAM_MESSAGE_THREAD_ID"],
    "email": ["EMAIL_RECEIVERS", "EMAIL_SENDER_NAME"],
    "wechat": ["WECHAT_MSG_TYPE"],
}


# DB channel 短字段名 -> 完整 config key（小写）
# 前端发 { channels: { telegram: { bot_token, chat_id } } }，需要映射为 telegram_bot_token / telegram_chat_id
_CHANNEL_SHORT_KEY_MAP: Dict[str, Dict[str, str]] = {
    "telegram": {
        "bot_token": "telegram_bot_token",
        "chat_id": "telegram_chat_id",
        "message_thread_id": "telegram_message_thread_id",
    },
    "wechat": {
        "webhook_url": "wechat_webhook_url",
        "msg_type": "wechat_msg_type",
    },
    "feishu": {
        "webhook_url": "feishu_webhook_url",
        "webhook_secret": "feishu_webhook_secret",
        "webhook_keyword": "feishu_webhook_keyword",
    },
    "email": {
        "sender": "email_sender",
        "password": "email_password",
        "receivers": "email_receivers",
        "sender_name": "email_sender_name",
    },
    "discord": {
        "webhook_url": "discord_webhook_url",
    },
    "slack": {
        "webhook_url": "slack_webhook_url",
    },
    "pushover": {
        "user_key": "pushover_user_key",
        "api_token": "pushover_api_token",
    },
    "ntfy": {
        "url": "ntfy_url",
    },
    "gotify": {
        "url": "gotify_url",
        "token": "gotify_token",
    },
    "pushplus": {
        "token": "pushplus_token",
    },
    "serverchan3": {
        "sendkey": "serverchan3_sendkey",
    },
    "custom": {
        "webhook_urls": "custom_webhook_urls",
    },
    "astrbot": {
        "url": "astrbot_url",
    },
}


def _build_config_from_db_and_env(
    db_config: Dict[str, Any],
) -> Dict[str, Any]:
    """从 DB 存储的 notification_config 和环境变量合并构建配置字典。

    DB config 优先级高于环境变量。
    key 统一用小写格式（telegram_bot_token），与 Sender 读取的 key 一致。

    Args:
        db_config: 从 user_llm_configs.notification_config 解析的 JSON 字典。

    Returns:
        合并后的配置字典，key 为小写格式。
    """
    config: Dict[str, Any] = {}

    # 1. 从环境变量读取默认值（大写 env var -> 存为小写 config key）
    all_env_keys: List[str] = []
    for keys in _CHANNEL_CONFIG_KEYS.values():
        all_env_keys.extend(keys)
    for keys in _CHANNEL_ADVANCED_KEYS.values():
        all_env_keys.extend(keys)
    all_env_keys.extend([
        "NOTIFICATION_DEDUP_TTL_SECONDS",
        "NOTIFICATION_COOLDOWN_SECONDS",
        "NOTIFICATION_QUIET_HOURS",
        "NOTIFICATION_TIMEZONE",
        "NOTIFICATION_MIN_SEVERITY",
    ])
    for route_config in NOTIFICATION_ROUTE_CONFIGS.values():
        all_env_keys.append(route_config["env_key"])

    for env_key in all_env_keys:
        val = os.environ.get(env_key)
        if val:
            config[env_key.lower()] = val

    # 2. 从 DB channel config 覆盖
    #    DB 存短字段名（bot_token），需要映射为完整名（telegram_bot_token）
    channels_cfg = db_config.get("channels", {})
    for channel_name, ch_cfg in channels_cfg.items():
        if not isinstance(ch_cfg, dict):
            continue
        # 用短 key 映射表转换
        short_map = _CHANNEL_SHORT_KEY_MAP.get(channel_name, {})
        for short_key, full_key in short_map.items():
            if short_key in ch_cfg and ch_cfg[short_key]:
                config[full_key] = ch_cfg[short_key]
        # 也支持 DB 直接存完整 key（telegram_bot_token）的情况
        for key_list in [
            _CHANNEL_CONFIG_KEYS.get(channel_name, []),
            _CHANNEL_ADVANCED_KEYS.get(channel_name, []),
        ]:
            for env_key in key_list:
                lower_key = env_key.lower()
                if lower_key in ch_cfg and ch_cfg[lower_key]:
                    config[lower_key] = ch_cfg[lower_key]

    # 3. 路由配置
    routes_cfg = db_config.get("routes", {})
    for route_type, route_def in NOTIFICATION_ROUTE_CONFIGS.items():
        env_key = route_def["env_key"]
        if route_type in routes_cfg and routes_cfg[route_type]:
            config[env_key.lower()] = routes_cfg[route_type]

    # 4. 噪音控制
    noise_cfg = db_config.get("noise", {})
    noise_key_map = {
        "dedup_ttl_seconds": "notification_dedup_ttl_seconds",
        "cooldown_seconds": "notification_cooldown_seconds",
        "quiet_hours": "notification_quiet_hours",
        "timezone": "notification_timezone",
        "min_severity": "notification_min_severity",
    }
    for db_key, env_key in noise_key_map.items():
        if db_key in noise_cfg and noise_cfg[db_key]:
            config[env_key] = str(noise_cfg[db_key])

    return config


def load_notification_config(
    db: "Session",
    user_id: str,
) -> Dict[str, Any]:
    """从 DB 加载用户的 notification_config JSON。"""
    from api.database import UserLLMConfigDB

    row = db.query(UserLLMConfigDB).filter(
        UserLLMConfigDB.user_id == user_id
    ).first()
    if row is None:
        return {}
    raw = getattr(row, "notification_config", None)
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        logger.warning("[notification] invalid notification_config JSON for user %s", user_id)
        return {}


def save_notification_config(
    db: "Session",
    user_id: str,
    config: Dict[str, Any],
) -> None:
    """将 notification_config 持久化到 DB。"""
    from api.database import UserLLMConfigDB

    row = db.query(UserLLMConfigDB).filter(
        UserLLMConfigDB.user_id == user_id
    ).first()
    if row is None:
        logger.error("[notification] user %s not found, cannot save config", user_id)
        return

    row.notification_config = json.dumps(config, ensure_ascii=False)
    db.commit()


def get_notification_config_response(
    db: "Session",
    user_id: str,
) -> Dict[str, Any]:
    """构建 GET /v1/notifications/config 响应。"""
    db_config = load_notification_config(db, user_id)

    # 渠道状态
    channels: Dict[str, Dict[str, Any]] = {}
    for channel_name, keys in _CHANNEL_CONFIG_KEYS.items():
        # 从 DB config 和环境变量合并
        merged = _build_config_from_db_and_env(db_config)
        detected = NotificationService.detect_configured_channels(merged)
        detected_names = [ch.value for ch in detected]

        ch_cfg = db_config.get("channels", {}).get(channel_name, {})
        is_enabled = ch_cfg.get("enabled", channel_name in detected_names)

        # 构建返回的 key 状态（不暴露实际值）
        # 也用短 key 映射表查找 DB 值
        short_map = _CHANNEL_SHORT_KEY_MAP.get(channel_name, {})
        reverse_short_map = {v: k for k, v in short_map.items()}  # telegram_bot_token -> bot_token
        key_status: Dict[str, Any] = {}
        for key in keys:
            lower_key = key.lower()
            # 查 DB: 先查完整 key (telegram_bot_token)，再查短 key (bot_token)
            db_val = ch_cfg.get(lower_key)
            if not db_val:
                short_key = reverse_short_map.get(lower_key)
                if short_key:
                    db_val = ch_cfg.get(short_key)
            has_val = bool(db_val) or bool(os.environ.get(key))
            key_status[lower_key] = {
                "configured": has_val,
                "display": "***" if has_val else None,
            }
        for key in _CHANNEL_ADVANCED_KEYS.get(channel_name, []):
            lower_key = key.lower()
            db_val = ch_cfg.get(lower_key)
            if not db_val:
                short_key = reverse_short_map.get(lower_key)
                if short_key:
                    db_val = ch_cfg.get(short_key)
            has_val = bool(db_val) or bool(os.environ.get(key))
            key_status[lower_key] = {
                "configured": has_val,
                "display": "***" if has_val else None,
            }

        channels[channel_name] = {
            "enabled": is_enabled,
            "display_name": ChannelDetector.get_channel_name(
                NotificationChannel(channel_name) if channel_name in [c.value for c in NotificationChannel]
                else NotificationChannel.UNKNOWN
            ),
            "keys": key_status,
        }

    # 路由配置
    routes: Dict[str, List[str]] = {}
    default_routes = db_config.get("routes", {})
    for route_type in NOTIFICATION_ROUTE_CONFIGS:
        routes[route_type] = default_routes.get(route_type, [])

    # 噪音控制
    noise_cfg = db_config.get("noise", {})

    return {
        "channels": channels,
        "routes": routes,
        "noise": {
            "dedup_ttl_seconds": noise_cfg.get("dedup_ttl_seconds", 600),
            "cooldown_seconds": noise_cfg.get("cooldown_seconds", 300),
            "quiet_hours": noise_cfg.get("quiet_hours", ""),
            "timezone": noise_cfg.get("timezone", "Asia/Shanghai"),
            "min_severity": noise_cfg.get("min_severity", "info"),
        },
        "available_channels": list(ROUTABLE_NOTIFICATION_CHANNELS),
        "route_configs": {
            k: {"description": v["description"], "env_key": v["env_key"]}
            for k, v in NOTIFICATION_ROUTE_CONFIGS.items()
        },
    }


def update_notification_config(
    db: "Session",
    user_id: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """更新用户的 notification_config 并返回新的完整配置。"""
    current = load_notification_config(db, user_id)

    # 合并 channels
    if "channels" in updates:
        existing_channels = current.get("channels", {})
        for ch_name, ch_cfg in updates["channels"].items():
            if ch_name in existing_channels:
                existing_channels[ch_name].update(ch_cfg)
            else:
                existing_channels[ch_name] = ch_cfg
        current["channels"] = existing_channels

    # 合并 routes
    if "routes" in updates:
        current.setdefault("routes", {}).update(updates["routes"])

    # 合并 noise
    if "noise" in updates:
        current.setdefault("noise", {}).update(updates["noise"])

    save_notification_config(db, user_id, current)
    return get_notification_config_response(db, user_id)


def test_notification_channel(
    channel: str,
    message: Optional[str] = None,
    db_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """测试发送指定渠道的通知。

    Args:
        channel: 渠道名称。
        message: 测试消息内容。
        db_config: DB 中的通知配置。

    Returns:
        {"sent": bool, "message": str, "channel": str}
    """
    from tradingagents.notification.senders import (
        EmailSender,
        WechatSender,
        FeishuSender,
        TelegramSender,
        DiscordSender,
        SlackSender,
    )

    test_msg = message or f"TradingAgents 通知测试\n渠道: {channel}\n这是一条测试消息。"

    # 构建配置
    config = _build_config_from_db_and_env(db_config or {})

    # 创建 NotificationService 并注册所有 sender
    service = NotificationService(config)
    sender_classes = [EmailSender, WechatSender, FeishuSender, TelegramSender, DiscordSender, SlackSender]
    for sender_cls in sender_classes:
        try:
            sender = sender_cls(config)
            service.register_sender(sender)
        except Exception as exc:
            logger.debug("[notification] failed to register sender %s: %s", sender_cls.__name__, exc)

    # 尝试发送
    try:
        result = service.send(
            test_msg,
            title="通知测试",
            target_channels=[channel],
            skip_noise_check=True,
        )
        if result.success:
            return {"sent": True, "message": f"测试消息已通过 {channel} 发送成功", "channel": channel}
        else:
            error_detail = ""
            for cr in result.channel_results:
                if cr.diagnostics:
                    error_detail = cr.diagnostics
                    break
            return {"sent": False, "message": f"发送失败: {error_detail or result.status}", "channel": channel}
    except Exception as exc:
        return {"sent": False, "message": f"发送异常: {exc}", "channel": channel}


def get_diagnostics(
    db_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """运行通知配置诊断。"""
    config = _build_config_from_db_and_env(db_config or {})
    result = run_notification_diagnostics(config)

    return {
        "ok": result.ok,
        "configured_channels": list(result.configured_channels),
        "errors": [
            {"severity": e.severity, "code": e.code, "message": e.message, "key": e.key}
            for e in result.errors
        ],
        "warnings": [
            {"severity": w.severity, "code": w.code, "message": w.message, "key": w.key}
            for w in result.warnings
        ],
        "info": [
            {"severity": i.severity, "code": i.code, "message": i.message, "key": i.key}
            for i in result.info
        ],
        "text": format_notification_diagnostics(result),
    }
