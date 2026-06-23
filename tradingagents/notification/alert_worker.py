# -*- coding: utf-8 -*-
"""
预警工作器

职责：
1. 在分析完成后触发预警检查
2. 评估分析结果是否匹配预警规则
3. 通过 NotificationService 发送预警通知
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from tradingagents.notification.alert_service import AlertRule, AlertService
from tradingagents.notification.core import NotificationService
from tradingagents.notification.noise import normalize_notification_severity

logger = logging.getLogger(__name__)


class AlertWorker:
    """预警工作器 —— 连接分析流水线与通知服务。

    使用示例::

        alert_service = AlertService()
        notification_service = NotificationService(config)
        worker = AlertWorker(alert_service, notification_service)

        # 分析完成后调用
        worker.on_analysis_complete("600519", {
            "signal": "看多",
            "price": 1800.0,
            "change_pct": 2.5,
        })
    """

    def __init__(
        self,
        alert_service: AlertService,
        notification_service: NotificationService,
    ) -> None:
        self._alert_service = alert_service
        self._notification_service = notification_service

    def on_analysis_complete(
        self,
        stock_code: str,
        analysis_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """分析完成回调 —— 评估预警规则并发送通知。

        Args:
            stock_code: 股票代码。
            analysis_result: 分析结果字典。

        Returns:
            已触发的预警列表（每项包含 rule_id, rule_name, send_result）。
        """
        # 1. 评估匹配的规则
        matched_rules = self._alert_service.evaluate(stock_code, analysis_result)
        if not matched_rules:
            return []

        logger.info("股票 %s 命中 %d 条预警规则", stock_code, len(matched_rules))

        # 2. 逐条发送通知
        triggered: List[Dict[str, Any]] = []
        for rule in matched_rules:
            content = self._build_alert_content(stock_code, analysis_result, rule)
            title = f"⚠️ 预警: {rule.name or stock_code}"

            send_result = self._notification_service.send(
                content,
                title=title,
                route_type=rule.route_type,
                target_channels=rule.channels or None,
                severity=rule.severity,
            )

            triggered.append({
                "rule_id": rule.id,
                "rule_name": rule.name,
                "stock_code": stock_code,
                "send_success": send_result.success,
                "send_status": send_result.status,
            })

            if send_result.success:
                logger.info("预警通知发送成功: %s -> %s", rule.name, stock_code)
            else:
                logger.warning("预警通知发送失败: %s -> %s (status=%s)",
                               rule.name, stock_code, send_result.status)

        return triggered

    @staticmethod
    def _build_alert_content(
        stock_code: str,
        result: Dict[str, Any],
        rule: AlertRule,
    ) -> str:
        """构建预警通知正文。"""
        lines = [
            f"**预警规则**: {rule.name}",
            f"**股票代码**: {stock_code}",
            f"**触发条件**: {rule.condition.value}" + (
                f" ({rule.condition_value})" if rule.condition_value else ""
            ),
        ]

        # 添加分析结果中的关键字段
        if "signal" in result:
            lines.append(f"**信号**: {result['signal']}")
        if "price" in result:
            lines.append(f"**当前价格**: {result['price']}")
        if "change_pct" in result:
            lines.append(f"**涨跌幅**: {result['change_pct']}%")
        if "summary" in result:
            lines.append("")
            lines.append(f"**摘要**: {result['summary']}")

        if rule.description:
            lines.append("")
            lines.append(f"**规则说明**: {rule.description}")

        return "\n".join(lines)
