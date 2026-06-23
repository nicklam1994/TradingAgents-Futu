# -*- coding: utf-8 -*-
"""进程内通知噪音控制助手。

本模块的状态是进程级别的，提供轻量级的去重/冷却/静默时段抑制，
无需持久化存储、文件锁或跨 worker 协调。

功能：
- 去重（Dedup）：基于内容哈希或自定义 key，在 TTL 内不重复发送
- 冷却（Cooldown）：同一 key 在冷却时间内不重复发送
- 静默时段（Quiet Hours）：指定时段内不发送
- 最低级别过滤（Min Severity）：低于指定级别的通知不发送
- 发送中保护（Inflight）：同一通知正在发送时，阻止重复发送
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

try:  # pragma: no cover - Python <3.9 fallback
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

NOTIFICATION_SEVERITIES: Tuple[str, ...] = ("info", "warning", "error", "critical")
NOTIFICATION_SEVERITY_RANK = {severity: index for index, severity in enumerate(NOTIFICATION_SEVERITIES)}

DEFAULT_NOTIFICATION_SEVERITY_BY_ROUTE = {
    "report": "info",
    "alert": "warning",
    "system_error": "error",
}

# P4 噪音控制相关的环境变量 key
P4_NOISE_ENV_KEYS: Tuple[str, ...] = (
    "NOTIFICATION_DEDUP_TTL_SECONDS",
    "NOTIFICATION_COOLDOWN_SECONDS",
    "NOTIFICATION_QUIET_HOURS",
    "NOTIFICATION_TIMEZONE",
    "NOTIFICATION_MIN_SEVERITY",
    "NOTIFICATION_DAILY_DIGEST_ENABLED",
)

_QUIET_HOURS_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
_INFLIGHT_RESERVATION_SECONDS = 300


# ---------------------------------------------------------------------------
# 决策数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NotificationNoiseDecision:
    """噪音控制门控返回的决策结果。"""

    should_send: bool
    reason_code: str = "allowed"
    message: str = ""
    route_type: str = "default"
    severity: str = "info"
    dedup_key: Optional[str] = None
    cooldown_key: Optional[str] = None
    dedup_ttl_seconds: int = 0
    cooldown_seconds: int = 0
    evaluated_at: Optional[datetime] = None
    dedup_reserved: bool = False
    cooldown_reserved: bool = False
    reservation_token: Optional[str] = None


# ---------------------------------------------------------------------------
# 进程内状态（线程安全）
# ---------------------------------------------------------------------------

_dedup_expires_at: Dict[str, float] = {}
_cooldown_expires_at: Dict[str, float] = {}
_dedup_inflight_until: Dict[str, Tuple[float, str]] = {}
_cooldown_inflight_until: Dict[str, Tuple[float, str]] = {}
_state_lock = threading.Lock()


def reset_notification_noise_state() -> None:
    """清空进程内噪音状态，供测试使用。"""
    with _state_lock:
        _dedup_expires_at.clear()
        _cooldown_expires_at.clear()
        _dedup_inflight_until.clear()
        _cooldown_inflight_until.clear()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def is_supported_notification_severity(value: object) -> bool:
    """判断 value 是否为支持的严重级别字符串。"""
    return str(value or "").strip().lower() in NOTIFICATION_SEVERITY_RANK


def normalize_notification_severity(route_type: Optional[str], severity: Optional[str] = None) -> str:
    """归一化显式严重级别，或从路由类型推导默认值。"""
    explicit = str(severity or "").strip().lower()
    if explicit in NOTIFICATION_SEVERITY_RANK:
        return explicit
    route = str(route_type or "").strip().lower()
    return DEFAULT_NOTIFICATION_SEVERITY_BY_ROUTE.get(route, "info")


def parse_notification_quiet_hours(value: Optional[str]) -> Optional[Tuple[int, int]]:
    """将 ``HH:MM-HH:MM`` 解析为 (start_minute_of_day, end_minute_of_day)。"""
    raw = str(value or "").strip()
    if not raw:
        return None

    match = _QUIET_HOURS_RE.match(raw)
    if not match:
        raise ValueError("NOTIFICATION_QUIET_HOURS must be in HH:MM-HH:MM format")

    start_hour, start_minute, end_hour, end_minute = [int(group) for group in match.groups()]
    return start_hour * 60 + start_minute, end_hour * 60 + end_minute


def validate_notification_timezone(value: Optional[str]) -> None:
    """验证可选的 IANA 时区名称。"""
    raw = str(value or "").strip()
    if not raw:
        return
    if ZoneInfo is None:
        raise ValueError("zoneinfo is unavailable")
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {raw}") from exc


def is_time_in_quiet_hours(now: datetime, quiet_hours: Tuple[int, int]) -> bool:
    """判断当前时间是否在静默时段内。"""
    start_minute, end_minute = quiet_hours
    minute_of_day = now.hour * 60 + now.minute

    if start_minute == end_minute:
        return False
    if start_minute < end_minute:
        return start_minute <= minute_of_day < end_minute
    # 跨午夜的情况（如 22:00-06:00）
    return minute_of_day >= start_minute or minute_of_day < end_minute


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _resolve_now(timezone_name: Optional[str], now: Optional[datetime]) -> datetime:
    """解析当前时间，支持时区。"""
    raw_timezone = str(timezone_name or "").strip()
    if raw_timezone:
        if ZoneInfo is None:
            raise ValueError("zoneinfo is unavailable")
        tz = ZoneInfo(raw_timezone)
        if now is None:
            return datetime.now(tz)
        if now.tzinfo is None:
            return now.replace(tzinfo=tz)
        return now.astimezone(tz)

    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is not None:
        return now.astimezone()
    return now


def _timestamp(now: datetime) -> float:
    return now.timestamp()


def _cleanup_expired(now_ts: float) -> None:
    """清理过期的状态条目。"""
    expired_dedup = [key for key, expires_at in _dedup_expires_at.items() if expires_at <= now_ts]
    for key in expired_dedup:
        _dedup_expires_at.pop(key, None)

    expired_cooldown = [key for key, expires_at in _cooldown_expires_at.items() if expires_at <= now_ts]
    for key in expired_cooldown:
        _cooldown_expires_at.pop(key, None)

    expired_dedup_inflight = [
        key
        for key, (expires_at, _token) in _dedup_inflight_until.items()
        if expires_at <= now_ts
    ]
    for key in expired_dedup_inflight:
        _dedup_inflight_until.pop(key, None)

    expired_cooldown_inflight = [
        key
        for key, (expires_at, _token) in _cooldown_inflight_until.items()
        if expires_at <= now_ts
    ]
    for key in expired_cooldown_inflight:
        _cooldown_inflight_until.pop(key, None)


def _stable_content_hash(content: str) -> str:
    """对内容生成稳定的 SHA-256 哈希。"""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _state_key(prefix: str, route_type: str, severity: str, key: str) -> str:
    return f"{prefix}:{route_type}:{severity}:{key}"


def _build_keys(
    *,
    content: str,
    route_type: str,
    severity: str,
    dedup_key: Optional[str],
    cooldown_key: Optional[str],
) -> Tuple[str, str]:
    dedup_part = str(dedup_key).strip() if dedup_key else _stable_content_hash(content)
    cooldown_part = str(cooldown_key).strip() if cooldown_key else "default"
    return (
        _state_key("dedup", route_type, severity, dedup_part),
        _state_key("cooldown", route_type, severity, cooldown_part),
    )


# ---------------------------------------------------------------------------
# 核心评估函数
# ---------------------------------------------------------------------------

def evaluate_notification_noise(
    config: object,
    *,
    content: str,
    route_type: Optional[str],
    severity: Optional[str] = None,
    dedup_key: Optional[str] = None,
    cooldown_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> NotificationNoiseDecision:
    """评估是否应该发送静态通知渠道。

    此函数是 fail-open 的：无效的运行时状态或意外异常会产生 allow 决策
    并记录警告日志，而不是阻断通知。
    """
    try:
        return _evaluate_notification_noise(
            config,
            content=content,
            route_type=route_type,
            severity=severity,
            dedup_key=dedup_key,
            cooldown_key=cooldown_key,
            now=now,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("通知降噪判断失败，将继续发送静态通知渠道: %s", exc)
        return NotificationNoiseDecision(
            should_send=True,
            reason_code="noise_check_failed_open",
            message="Noise-control check failed open.",
            route_type=str(route_type or "default").strip().lower() or "default",
            severity=normalize_notification_severity(route_type, severity),
        )


def _evaluate_notification_noise(
    config: object,
    *,
    content: str,
    route_type: Optional[str],
    severity: Optional[str],
    dedup_key: Optional[str],
    cooldown_key: Optional[str],
    now: Optional[datetime],
) -> NotificationNoiseDecision:
    route = str(route_type or "default").strip().lower() or "default"
    resolved_severity = normalize_notification_severity(route, severity)
    dedup_ttl = max(0, int(getattr(config, "notification_dedup_ttl_seconds", 0) or 0))
    cooldown = max(0, int(getattr(config, "notification_cooldown_seconds", 0) or 0))
    quiet_hours_raw = getattr(config, "notification_quiet_hours", "") or ""
    timezone_name = getattr(config, "notification_timezone", "") or ""
    min_severity_raw = str(getattr(config, "notification_min_severity", "") or "").strip().lower()

    effective_now = _resolve_now(timezone_name, now)
    now_ts = _timestamp(effective_now)
    decision_base = {
        "route_type": route,
        "severity": resolved_severity,
        "dedup_ttl_seconds": dedup_ttl,
        "cooldown_seconds": cooldown,
        "evaluated_at": effective_now,
    }

    # 1. 最低级别过滤
    if min_severity_raw:
        if min_severity_raw not in NOTIFICATION_SEVERITY_RANK:
            logger.warning("NOTIFICATION_MIN_SEVERITY=%s 无效，将忽略最低级别过滤", min_severity_raw)
        elif NOTIFICATION_SEVERITY_RANK[resolved_severity] < NOTIFICATION_SEVERITY_RANK[min_severity_raw]:
            return NotificationNoiseDecision(
                should_send=False,
                reason_code="min_severity",
                message=(
                    f"通知级别 {resolved_severity} 低于最低级别 {min_severity_raw}，"
                    "已跳过静态通知渠道。"
                ),
                **decision_base,  # type: ignore[arg-type]
            )

    # 2. 静默时段
    quiet_hours = parse_notification_quiet_hours(quiet_hours_raw)
    if quiet_hours and is_time_in_quiet_hours(effective_now, quiet_hours):
        return NotificationNoiseDecision(
            should_send=False,
            reason_code="quiet_hours",
            message=f"当前时间处于静默时段 {quiet_hours_raw}，已跳过静态通知渠道。",
            **decision_base,  # type: ignore[arg-type]
        )

    # 3. 去重 + 冷却
    dedup_state_key, cooldown_state_key = _build_keys(
        content=content,
        route_type=route,
        severity=resolved_severity,
        dedup_key=dedup_key,
        cooldown_key=cooldown_key,
    )
    with _state_lock:
        _cleanup_expired(now_ts)
        # 去重 TTL 内已发送
        if dedup_ttl > 0 and _dedup_expires_at.get(dedup_state_key, 0) > now_ts:
            return NotificationNoiseDecision(
                should_send=False,
                reason_code="dedup",
                message="通知内容在去重 TTL 内已发送，已跳过静态通知渠道。",
                dedup_key=dedup_state_key,
                cooldown_key=cooldown_state_key,
                **decision_base,  # type: ignore[arg-type]
            )
        # 去重 inflight
        dedup_inflight = _dedup_inflight_until.get(dedup_state_key)
        if dedup_ttl > 0 and dedup_inflight and dedup_inflight[0] > now_ts:
            return NotificationNoiseDecision(
                should_send=False,
                reason_code="dedup_inflight",
                message="同一通知正在发送中，已跳过静态通知渠道。",
                dedup_key=dedup_state_key,
                cooldown_key=cooldown_state_key,
                **decision_base,  # type: ignore[arg-type]
            )
        # 冷却时间
        if cooldown > 0 and _cooldown_expires_at.get(cooldown_state_key, 0) > now_ts:
            return NotificationNoiseDecision(
                should_send=False,
                reason_code="cooldown",
                message="通知冷却时间尚未结束，已跳过静态通知渠道。",
                dedup_key=dedup_state_key,
                cooldown_key=cooldown_state_key,
                **decision_base,  # type: ignore[arg-type]
            )
        # 冷却 inflight
        cooldown_inflight = _cooldown_inflight_until.get(cooldown_state_key)
        if cooldown > 0 and cooldown_inflight and cooldown_inflight[0] > now_ts:
            return NotificationNoiseDecision(
                should_send=False,
                reason_code="cooldown_inflight",
                message="同一通知正在发送中，已跳过静态通知渠道。",
                dedup_key=dedup_state_key,
                cooldown_key=cooldown_state_key,
                **decision_base,  # type: ignore[arg-type]
            )

        # 预留 inflight 槽位
        reservation_until = now_ts + _INFLIGHT_RESERVATION_SECONDS
        dedup_reserved = dedup_ttl > 0
        cooldown_reserved = cooldown > 0
        reservation_token = uuid.uuid4().hex if dedup_reserved or cooldown_reserved else None
        if dedup_reserved:
            _dedup_inflight_until[dedup_state_key] = (reservation_until, reservation_token)  # type: ignore[arg-type]
        if cooldown_reserved:
            _cooldown_inflight_until[cooldown_state_key] = (reservation_until, reservation_token)  # type: ignore[arg-type]

    return NotificationNoiseDecision(
        should_send=True,
        dedup_key=dedup_state_key,
        cooldown_key=cooldown_state_key,
        dedup_reserved=dedup_reserved,
        cooldown_reserved=cooldown_reserved,
        reservation_token=reservation_token,
        **decision_base,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 预留释放 + 状态记录
# ---------------------------------------------------------------------------

def _release_reserved_locked(decision: NotificationNoiseDecision) -> None:
    """释放 inflight 预留（需持有 _state_lock）。"""
    if decision.dedup_reserved and decision.dedup_key:
        dedup_inflight = _dedup_inflight_until.get(decision.dedup_key)
        if dedup_inflight and dedup_inflight[1] == decision.reservation_token:
            _dedup_inflight_until.pop(decision.dedup_key, None)
    if decision.cooldown_reserved and decision.cooldown_key:
        cooldown_inflight = _cooldown_inflight_until.get(decision.cooldown_key)
        if cooldown_inflight and cooldown_inflight[1] == decision.reservation_token:
            _cooldown_inflight_until.pop(decision.cooldown_key, None)


def release_notification_noise(decision: NotificationNoiseDecision) -> None:
    """释放 inflight 预留，不记录去重/冷却状态。"""
    if not decision.should_send:
        return
    try:
        with _state_lock:
            _release_reserved_locked(decision)
    except Exception as exc:  # pragma: no cover
        logger.warning("通知降噪发送中状态释放失败，忽略该错误: %s", exc)


def record_notification_noise(decision: NotificationNoiseDecision, now: Optional[datetime] = None) -> None:
    """在静态通知发送成功后记录去重/冷却状态。"""
    if not decision.should_send or decision.evaluated_at is None:
        return
    try:
        record_at = now
        if record_at is None:
            record_at = datetime.now(decision.evaluated_at.tzinfo)
        now_ts = _timestamp(record_at)
        with _state_lock:
            _cleanup_expired(now_ts)
            _release_reserved_locked(decision)
            if decision.dedup_ttl_seconds > 0 and decision.dedup_key:
                _dedup_expires_at[decision.dedup_key] = now_ts + decision.dedup_ttl_seconds
            if decision.cooldown_seconds > 0 and decision.cooldown_key:
                _cooldown_expires_at[decision.cooldown_key] = now_ts + decision.cooldown_seconds
    except Exception as exc:  # pragma: no cover
        logger.warning("通知降噪状态记录失败，忽略该错误: %s", exc)
