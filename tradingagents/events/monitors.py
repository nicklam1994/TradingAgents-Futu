# -*- coding: utf-8 -*-
"""
事件监控器

提供三种市场数据检测器：
- VolumeSpikeMonitor  — 成交量突增检测（当前量 vs N 日均量，阈值倍数）
- PriceChangeMonitor  — 价格变动百分比触发（vs 前一日收盘价）
- SentimentMonitor    — 情绪阈值触发（需要情绪数据源）

每个 Monitor 继承 BaseMonitor：
- check(data) -> Optional[MonitorAlert]  — 核心检测逻辑
- to_alert_rule() -> AlertRule            — 转换为 TAF 的 AlertRule 格式

设计原则：
- Monitor 是纯函数式检测器，不持有状态，不依赖数据库
- 检测结果 MonitorAlert 是不可变数据，可序列化
- to_alert_rule() 桥接到 TAF 通知系统，复用 AlertService + AlertWorker
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from tradingagents.notification.alert_service import (
    AlertCondition,
    AlertRule,
    AlertStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MonitorAlert — 检测结果数据结构
# ---------------------------------------------------------------------------


class MonitorType(str, Enum):
    """监控器类型"""
    VOLUME_SPIKE = "volume_spike"
    PRICE_CHANGE = "price_change"
    SENTIMENT = "sentiment"


@dataclass(frozen=True)
class MonitorAlert:
    """监控器触发后的检测结果。

    不可变数据，可安全传递和序列化。
    """
    monitor_type: MonitorType
    stock_code: str
    triggered: bool
    observed_value: float
    threshold: float
    message: str
    severity: str = "warning"  # info / warning / error / critical
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "monitor_type": self.monitor_type.value,
            "stock_code": self.stock_code,
            "triggered": self.triggered,
            "observed_value": self.observed_value,
            "threshold": self.threshold,
            "message": self.message,
            "severity": self.severity,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# BaseMonitor — 抽象基类
# ---------------------------------------------------------------------------


class BaseMonitor(ABC):
    """监控器抽象基类。

    子类必须实现：
    - check(data) -> Optional[MonitorAlert]
    - to_alert_rule() -> AlertRule
    """

    @abstractmethod
    def check(self, data: Dict[str, Any]) -> Optional[MonitorAlert]:
        """核心检测逻辑。

        Args:
            data: 市场数据字典，字段由子类定义。

        Returns:
            MonitorAlert 如果触发检测，否则 None。
        """
        ...

    @abstractmethod
    def to_alert_rule(self) -> AlertRule:
        """将监控器配置转换为 TAF AlertRule。

        转换后的 AlertRule 可直接插入 AlertService，
        由 AlertWorker 在评估周期中批量处理。
        """
        ...


# ---------------------------------------------------------------------------
# VolumeSpikeMonitor — 成交量突增检测
# ---------------------------------------------------------------------------


class VolumeSpikeMonitor(BaseMonitor):
    """成交量突增检测器。

    检测逻辑：当前成交量 / N 日平均成交量 >= 阈值倍数

    使用示例::

        monitor = VolumeSpikeMonitor(
            stock_code="600519",
            lookback_days=20,
            spike_multiplier=2.0,
        )
        alert = monitor.check({
            "current_volume": 1_000_000,
            "avg_volume": 400_000,
        })
        if alert and alert.triggered:
            rule = monitor.to_alert_rule()
            alert_service.create_rule(rule)
    """

    def __init__(
        self,
        stock_code: str,
        lookback_days: int = 20,
        spike_multiplier: float = 2.0,
        severity: str = "warning",
    ) -> None:
        if lookback_days < 1:
            raise ValueError(f"lookback_days must be >= 1, got {lookback_days}")
        if spike_multiplier <= 0:
            raise ValueError(f"spike_multiplier must be > 0, got {spike_multiplier}")

        self.stock_code = stock_code
        self.lookback_days = lookback_days
        self.spike_multiplier = spike_multiplier
        self.severity = severity

    def check(self, data: Dict[str, Any]) -> Optional[MonitorAlert]:
        """检测成交量是否突增。

        Args:
            data: 必须包含:
                - current_volume (float): 当前成交量
                - avg_volume (float): N 日平均成交量

        Returns:
            MonitorAlert，triggered=True 表示检测到突增。
        """
        current_volume = data.get("current_volume")
        avg_volume = data.get("avg_volume")

        # 数据不完整时返回未触发
        if current_volume is None or avg_volume is None:
            logger.debug(
                "[%s] VolumeSpike: 缺少数据 current_volume=%s, avg_volume=%s",
                self.stock_code, current_volume, avg_volume,
            )
            return None

        if avg_volume <= 0:
            logger.debug(
                "[%s] VolumeSpike: avg_volume <= 0 (%s), 跳过",
                self.stock_code, avg_volume,
            )
            return MonitorAlert(
                monitor_type=MonitorType.VOLUME_SPIKE,
                stock_code=self.stock_code,
                triggered=False,
                observed_value=0.0,
                threshold=self.spike_multiplier,
                message=f"{self.stock_code} avg_volume <= 0, 无法计算",
                severity=self.severity,
                metadata={"lookback_days": self.lookback_days},
            )

        ratio = float(current_volume) / float(avg_volume)
        triggered = ratio >= self.spike_multiplier

        if triggered:
            msg = (
                f"{self.stock_code} 成交量突增 {ratio:.1f}x "
                f"(>= {self.spike_multiplier:.1f}x 阈值, "
                f"当前 {current_volume:,.0f} / 均量 {avg_volume:,.0f})"
            )
        else:
            msg = (
                f"{self.stock_code} 成交量正常 {ratio:.1f}x "
                f"(< {self.spike_multiplier:.1f}x 阈值)"
            )

        return MonitorAlert(
            monitor_type=MonitorType.VOLUME_SPIKE,
            stock_code=self.stock_code,
            triggered=triggered,
            observed_value=ratio,
            threshold=self.spike_multiplier,
            message=msg,
            severity=self.severity if triggered else "info",
            metadata={
                "lookback_days": self.lookback_days,
                "current_volume": float(current_volume),
                "avg_volume": float(avg_volume),
            },
        )

    def to_alert_rule(self) -> AlertRule:
        """转换为 TAF AlertRule（volume_spike 条件）。"""
        return AlertRule(
            id=uuid.uuid4().hex[:12],
            name=f"VolumeSpike_{self.stock_code}",
            description=(
                f"成交量突增监控: {self.stock_code}, "
                f"回看 {self.lookback_days} 日, "
                f"阈值 {self.spike_multiplier}x"
            ),
            status=AlertStatus.ACTIVE,
            stock_codes=[self.stock_code],
            condition=AlertCondition.CHANGE_ABOVE,  # 复用已有条件类型
            condition_value=str(self.spike_multiplier),
            severity=self.severity,
            channels=[],
            route_type="alert",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )


# ---------------------------------------------------------------------------
# PriceChangeMonitor — 价格变动检测
# ---------------------------------------------------------------------------


class PriceChangeMonitor(BaseMonitor):
    """价格变动百分比检测器。

    检测逻辑：abs(当前价 - 前收盘) / 前收盘 * 100 >= 阈值百分比

    使用示例::

        monitor = PriceChangeMonitor(
            stock_code="600519",
            change_threshold_pct=5.0,
        )
        alert = monitor.check({
            "current_price": 1900.0,
            "prev_close": 1800.0,
        })
    """

    def __init__(
        self,
        stock_code: str,
        change_threshold_pct: float = 5.0,
        severity: str = "warning",
    ) -> None:
        if change_threshold_pct <= 0:
            raise ValueError(
                f"change_threshold_pct must be > 0, got {change_threshold_pct}"
            )

        self.stock_code = stock_code
        self.change_threshold_pct = change_threshold_pct
        self.severity = severity

    def check(self, data: Dict[str, Any]) -> Optional[MonitorAlert]:
        """检测价格变动是否超阈值。

        Args:
            data: 必须包含:
                - current_price (float): 当前价格
                - prev_close (float): 前一日收盘价

        Returns:
            MonitorAlert，triggered=True 表示检测到异动。
        """
        current_price = data.get("current_price")
        prev_close = data.get("prev_close")

        if current_price is None or prev_close is None:
            logger.debug(
                "[%s] PriceChange: 缺少数据 current_price=%s, prev_close=%s",
                self.stock_code, current_price, prev_close,
            )
            return None

        if prev_close <= 0:
            logger.debug(
                "[%s] PriceChange: prev_close <= 0 (%s), 跳过",
                self.stock_code, prev_close,
            )
            return None

        change_pct = (float(current_price) - float(prev_close)) / float(prev_close) * 100.0
        abs_change = abs(change_pct)
        triggered = abs_change >= self.change_threshold_pct

        direction = "涨" if change_pct >= 0 else "跌"
        if triggered:
            msg = (
                f"{self.stock_code} {direction} {abs_change:.2f}% "
                f"(>= ±{self.change_threshold_pct:.1f}% 阈值, "
                f"当前 {current_price:.2f} / 昨收 {prev_close:.2f})"
            )
        else:
            msg = (
                f"{self.stock_code} {direction} {abs_change:.2f}% "
                f"(< ±{self.change_threshold_pct:.1f}% 阈值)"
            )

        return MonitorAlert(
            monitor_type=MonitorType.PRICE_CHANGE,
            stock_code=self.stock_code,
            triggered=triggered,
            observed_value=change_pct,
            threshold=self.change_threshold_pct,
            message=msg,
            severity=self.severity if triggered else "info",
            metadata={
                "current_price": float(current_price),
                "prev_close": float(prev_close),
                "change_pct": change_pct,
            },
        )

    def to_alert_rule(self) -> AlertRule:
        """转换为 TAF AlertRule（CHANGE_ABOVE 条件）。"""
        return AlertRule(
            id=uuid.uuid4().hex[:12],
            name=f"PriceChange_{self.stock_code}",
            description=(
                f"价格异动监控: {self.stock_code}, "
                f"阈值 ±{self.change_threshold_pct}%"
            ),
            status=AlertStatus.ACTIVE,
            stock_codes=[self.stock_code],
            condition=AlertCondition.CHANGE_ABOVE,
            condition_value=str(self.change_threshold_pct),
            severity=self.severity,
            channels=[],
            route_type="alert",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )


# ---------------------------------------------------------------------------
# SentimentMonitor — 情绪阈值检测
# ---------------------------------------------------------------------------


class SentimentMonitor(BaseMonitor):
    """情绪阈值检测器。

    检测逻辑：abs(情绪分数) >= 阈值

    情绪分数范围通常为 [-1.0, 1.0]：
    - 正值 = 看多情绪
    - 负值 = 看空情绪
    - 绝对值越大 = 情绪越极端

    使用示例::

        monitor = SentimentMonitor(
            stock_code="600519",
            sentiment_threshold=0.7,
        )
        alert = monitor.check({
            "sentiment_score": -0.85,
        })
    """

    def __init__(
        self,
        stock_code: str,
        sentiment_threshold: float = 0.7,
        severity: str = "warning",
    ) -> None:
        if not (0 < sentiment_threshold <= 2.0):
            raise ValueError(
                f"sentiment_threshold must be in (0, 2.0], got {sentiment_threshold}"
            )

        self.stock_code = stock_code
        self.sentiment_threshold = sentiment_threshold
        self.severity = severity

    def check(self, data: Dict[str, Any]) -> Optional[MonitorAlert]:
        """检测情绪是否达到阈值。

        Args:
            data: 必须包含:
                - sentiment_score (float): 情绪分数，范围 [-1, 1]

        Returns:
            MonitorAlert，triggered=True 表示情绪极端。
        """
        sentiment_score = data.get("sentiment_score")

        if sentiment_score is None:
            logger.debug(
                "[%s] Sentiment: 缺少 sentiment_score", self.stock_code,
            )
            return None

        abs_score = abs(float(sentiment_score))
        triggered = abs_score >= self.sentiment_threshold

        direction = "看多" if float(sentiment_score) >= 0 else "看空"
        if triggered:
            msg = (
                f"{self.stock_code} 情绪极端: {direction} {abs_score:.2f} "
                f"(>= ±{self.sentiment_threshold:.2f} 阈值)"
            )
        else:
            msg = (
                f"{self.stock_code} 情绪平稳: {direction} {abs_score:.2f} "
                f"(< ±{self.sentiment_threshold:.2f} 阈值)"
            )

        return MonitorAlert(
            monitor_type=MonitorType.SENTIMENT,
            stock_code=self.stock_code,
            triggered=triggered,
            observed_value=float(sentiment_score),
            threshold=self.sentiment_threshold,
            message=msg,
            severity=self.severity if triggered else "info",
            metadata={
                "sentiment_score": float(sentiment_score),
                "direction": direction,
            },
        )

    def to_alert_rule(self) -> AlertRule:
        """转换为 TAF AlertRule（SENTIMENT_CHANGE 条件）。

        注意：tradingagents/notification/alert_service.py 的 AlertCondition
        没有 SENTIMENT_CHANGE，这里复用 CHANGE_ABOVE 并在 description 中标注。
        如果后续 AlertCondition 扩展了情绪类型，可直接替换。
        """
        return AlertRule(
            id=uuid.uuid4().hex[:12],
            name=f"Sentiment_{self.stock_code}",
            description=(
                f"情绪监控: {self.stock_code}, "
                f"阈值 ±{self.sentiment_threshold}"
            ),
            status=AlertStatus.ACTIVE,
            stock_codes=[self.stock_code],
            # 复用 CHANGE_ABOVE，实际语义由 description 区分
            condition=AlertCondition.CHANGE_ABOVE,
            condition_value=str(self.sentiment_threshold),
            severity=self.severity,
            channels=[],
            route_type="alert",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
