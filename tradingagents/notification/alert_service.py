# -*- coding: utf-8 -*-
"""
预警服务

职责：
1. AlertRule 数据结构 —— 定义预警规则
2. AlertService —— 预警规则 CRUD + 评估触发
3. 支持按股票代码、信号类型、严重级别等条件匹配
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AlertRule 数据结构
# ---------------------------------------------------------------------------

class AlertStatus(str, Enum):
    """预警规则状态"""
    ACTIVE = "active"
    PAUSED = "paused"
    TRIGGERED = "triggered"


class AlertCondition(str, Enum):
    """预警触发条件类型"""
    SIGNAL_MATCH = "signal_match"          # 信号匹配（如：看多/看空）
    PRICE_ABOVE = "price_above"            # 价格高于阈值
    PRICE_BELOW = "price_below"            # 价格低于阈值
    CHANGE_ABOVE = "change_above"          # 涨跌幅高于阈值（%）
    CHANGE_BELOW = "change_below"          # 涨跌幅低于阈值（%）
    ANY_ANALYSIS = "any_analysis"          # 任意分析完成


@dataclass
class AlertRule:
    """预警规则定义。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    description: str = ""
    status: AlertStatus = AlertStatus.ACTIVE

    # 匹配条件
    stock_codes: List[str] = field(default_factory=list)  # 空 = 全部股票
    condition: AlertCondition = AlertCondition.ANY_ANALYSIS
    condition_value: str = ""  # 条件参数（信号名/价格/百分比）
    severity: str = "warning"  # info / warning / error / critical

    # 通知配置
    channels: List[str] = field(default_factory=list)  # 目标渠道，空 = 使用默认路由
    route_type: str = "alert"  # 路由类型

    # 元数据
    created_at: str = ""
    updated_at: str = ""
    last_triggered_at: Optional[str] = None
    trigger_count: int = 0

    def matches_stock(self, stock_code: str) -> bool:
        """检查股票代码是否匹配此规则。"""
        if not self.stock_codes:
            return True  # 空 = 匹配所有
        return stock_code in self.stock_codes

    def matches_condition(self, analysis_result: Dict[str, Any]) -> bool:
        """检查分析结果是否满足触发条件。"""
        if self.condition == AlertCondition.ANY_ANALYSIS:
            return True

        if self.condition == AlertCondition.SIGNAL_MATCH:
            signal = str(analysis_result.get("signal", "")).lower()
            return signal == self.condition_value.lower()

        if self.condition in (AlertCondition.PRICE_ABOVE, AlertCondition.PRICE_BELOW):
            price = analysis_result.get("price")
            if price is None:
                return False
            try:
                threshold = float(self.condition_value)
                if self.condition == AlertCondition.PRICE_ABOVE:
                    return float(price) > threshold
                return float(price) < threshold
            except (ValueError, TypeError):
                return False

        if self.condition in (AlertCondition.CHANGE_ABOVE, AlertCondition.CHANGE_BELOW):
            change = analysis_result.get("change_pct")
            if change is None:
                return False
            try:
                threshold = float(self.condition_value)
                if self.condition == AlertCondition.CHANGE_ABOVE:
                    return float(change) > threshold
                return float(change) < threshold
            except (ValueError, TypeError):
                return False

        return False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AlertRule":
        """从字典反序列化，忽略未知字段。"""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        # 枚举类型转换
        if "status" in filtered and isinstance(filtered["status"], str):
            filtered["status"] = AlertStatus(filtered["status"])
        if "condition" in filtered and isinstance(filtered["condition"], str):
            filtered["condition"] = AlertCondition(filtered["condition"])
        return cls(**filtered)


# ---------------------------------------------------------------------------
# AlertService —— CRUD + 评估
# ---------------------------------------------------------------------------

class AlertService:
    """预警规则管理服务。

    规则持久化到 JSON 文件，默认路径: ~/.tradingagents/alert_rules.json
    线程安全。使用模块级锁保护单例创建，避免 TOCTOU 竞态。
    """

    # 类级锁：保护单例创建（B4 修复）
    _singleton_lock = threading.Lock()
    _instances: Dict[str, "AlertService"] = {}

    def __new__(cls, rules_path: Optional[str] = None) -> "AlertService":
        """单例工厂：同一 rules_path 只创建一个实例。"""
        path = rules_path or os.path.expanduser("~/.tradingagents/alert_rules.json")
        with cls._singleton_lock:
            if path in cls._instances:
                return cls._instances[path]
            instance = super().__new__(cls)
            cls._instances[path] = instance
            return instance

    def __init__(self, rules_path: Optional[str] = None) -> None:
        # 防止 __new__ 返回已有实例时重复初始化
        if hasattr(self, "_initialized"):
            return
        self._rules_path = rules_path or os.path.expanduser("~/.tradingagents/alert_rules.json")
        self._rules: Dict[str, AlertRule] = {}
        self._lock = threading.Lock()
        self._load_rules()
        self._initialized = True

    @classmethod
    def _reset_singletons(cls) -> None:
        """清空单例缓存，仅用于测试。"""
        with cls._singleton_lock:
            cls._instances.clear()

    # --- 持久化 ---

    def _load_rules(self) -> None:
        """从磁盘加载规则。"""
        if not os.path.exists(self._rules_path):
            return
        try:
            with open(self._rules_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                rule = AlertRule.from_dict(item)
                self._rules[rule.id] = rule
            logger.info("已加载 %d 条预警规则: %s", len(self._rules), self._rules_path)
        except Exception as exc:
            logger.error("加载预警规则失败: %s", exc)

    def _save_rules(self) -> None:
        """持久化规则到磁盘（原子写入）。

        先写入同目录临时文件，再 os.rename() 替换目标文件。
        rename 在 POSIX 上是原子操作，崩溃时不会损坏原文件。
        """
        try:
            dir_name = os.path.dirname(self._rules_path)
            os.makedirs(dir_name, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump([r.to_dict() for r in self._rules.values()], f,
                              ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp_path, self._rules_path)
            except Exception:
                # 清理临时文件，避免残留
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.error("保存预警规则失败: %s", exc)

    # --- CRUD ---

    def create_rule(self, rule: AlertRule) -> AlertRule:
        """创建预警规则。"""
        now = datetime.now().isoformat()
        rule.created_at = now
        rule.updated_at = now
        with self._lock:
            self._rules[rule.id] = rule
            self._save_rules()
        logger.info("创建预警规则: %s (%s)", rule.name, rule.id)
        return rule

    def get_rule(self, rule_id: str) -> Optional[AlertRule]:
        """获取单条规则。"""
        with self._lock:
            return self._rules.get(rule_id)

    def list_rules(
        self,
        *,
        status: Optional[AlertStatus] = None,
        stock_code: Optional[str] = None,
    ) -> List[AlertRule]:
        """列出规则，支持过滤。"""
        with self._lock:
            rules = list(self._rules.values())
        if status:
            rules = [r for r in rules if r.status == status]
        if stock_code:
            rules = [r for r in rules if r.matches_stock(stock_code)]
        return rules

    def update_rule(self, rule_id: str, **kwargs: Any) -> Optional[AlertRule]:
        """更新规则字段。"""
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                return None
            for key, value in kwargs.items():
                if hasattr(rule, key):
                    setattr(rule, key, value)
            rule.updated_at = datetime.now().isoformat()
            self._save_rules()
        logger.info("更新预警规则: %s (%s)", rule.name, rule_id)
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        """删除规则。"""
        with self._lock:
            if rule_id in self._rules:
                del self._rules[rule_id]
                self._save_rules()
                logger.info("删除预警规则: %s", rule_id)
                return True
        return False

    # --- 评估 ---

    def evaluate(
        self,
        stock_code: str,
        analysis_result: Dict[str, Any],
    ) -> List[AlertRule]:
        """评估分析结果，返回所有匹配的活跃规则。

        Args:
            stock_code: 股票代码。
            analysis_result: 分析结果字典，至少包含 signal, price, change_pct 等。

        Returns:
            匹配的 AlertRule 列表。
        """
        now = datetime.now().isoformat()
        matched: List[AlertRule] = []

        with self._lock:
            for rule in self._rules.values():
                if rule.status != AlertStatus.ACTIVE:
                    continue
                if not rule.matches_stock(stock_code):
                    continue
                if not rule.matches_condition(analysis_result):
                    continue
                # 命中
                rule.last_triggered_at = now
                rule.trigger_count += 1
                matched.append(rule)

            if matched:
                self._save_rules()

        return matched
