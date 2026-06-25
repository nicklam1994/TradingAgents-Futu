# -*- coding: utf-8 -*-
"""
事件监控器测试

覆盖：
- VolumeSpikeMonitor: 正常、突增、边界条件、数据缺失
- PriceChangeMonitor: 涨、跌、平、数据缺失
- SentimentMonitor: 看多、看空、中性、数据缺失
- to_alert_rule(): AlertRule 转换验证
- 参数校验: 构造函数异常
"""

from __future__ import annotations

import pytest

from tradingagents.events.monitors import (
    BaseMonitor,
    MonitorAlert,
    MonitorType,
    PriceChangeMonitor,
    SentimentMonitor,
    VolumeSpikeMonitor,
)
from tradingagents.notification.alert_service import AlertCondition, AlertStatus


# ---------------------------------------------------------------------------
# VolumeSpikeMonitor 测试
# ---------------------------------------------------------------------------


class TestVolumeSpikeMonitor:
    """成交量突增检测器测试。"""

    def test_spike_triggers_when_ratio_exceeds_threshold(self):
        """成交量 >= 2x 均量时应触发。"""
        monitor = VolumeSpikeMonitor("600519", lookback_days=20, spike_multiplier=2.0)
        alert = monitor.check({
            "current_volume": 1_000_000,
            "avg_volume": 400_000,
        })
        assert alert is not None
        assert alert.triggered is True
        assert alert.observed_value == pytest.approx(2.5)
        assert alert.threshold == 2.0
        assert "突增" in alert.message
        assert alert.severity == "warning"

    def test_no_spike_when_ratio_below_threshold(self):
        """成交量 < 2x 均量时不应触发。"""
        monitor = VolumeSpikeMonitor("600519", spike_multiplier=2.0)
        alert = monitor.check({
            "current_volume": 500_000,
            "avg_volume": 400_000,
        })
        assert alert is not None
        assert alert.triggered is False
        assert alert.observed_value == pytest.approx(1.25)
        assert "正常" in alert.message

    def test_spike_at_exact_threshold(self):
        """成交量恰好等于阈值倍数时应触发（>= 边界）。"""
        monitor = VolumeSpikeMonitor("600519", spike_multiplier=3.0)
        alert = monitor.check({
            "current_volume": 900_000,
            "avg_volume": 300_000,
        })
        assert alert is not None
        assert alert.triggered is True
        assert alert.observed_value == pytest.approx(3.0)

    def test_missing_current_volume_returns_none(self):
        """缺少 current_volume 时返回 None。"""
        monitor = VolumeSpikeMonitor("600519")
        alert = monitor.check({"avg_volume": 400_000})
        assert alert is None

    def test_missing_avg_volume_returns_none(self):
        """缺少 avg_volume 时返回 None。"""
        monitor = VolumeSpikeMonitor("600519")
        alert = monitor.check({"current_volume": 1_000_000})
        assert alert is None

    def test_avg_volume_zero_returns_not_triggered(self):
        """avg_volume 为 0 时返回未触发（避免除零）。"""
        monitor = VolumeSpikeMonitor("600519")
        alert = monitor.check({
            "current_volume": 1_000_000,
            "avg_volume": 0,
        })
        assert alert is not None
        assert alert.triggered is False
        assert alert.observed_value == 0.0

    def test_avg_volume_negative_returns_not_triggered(self):
        """avg_volume 为负数时返回未触发。"""
        monitor = VolumeSpikeMonitor("600519")
        alert = monitor.check({
            "current_volume": 1_000_000,
            "avg_volume": -100,
        })
        assert alert is not None
        assert alert.triggered is False

    def test_empty_data_returns_none(self):
        """空数据字典返回 None。"""
        monitor = VolumeSpikeMonitor("600519")
        alert = monitor.check({})
        assert alert is None

    def test_custom_severity(self):
        """自定义 severity 应正确传递。"""
        monitor = VolumeSpikeMonitor("600519", severity="critical")
        alert = monitor.check({
            "current_volume": 2_000_000,
            "avg_volume": 500_000,
        })
        assert alert is not None
        assert alert.severity == "critical"

    def test_metadata_contains_lookback_days(self):
        """metadata 应包含 lookback_days。"""
        monitor = VolumeSpikeMonitor("600519", lookback_days=10)
        alert = monitor.check({
            "current_volume": 1_000_000,
            "avg_volume": 400_000,
        })
        assert alert is not None
        assert alert.metadata["lookback_days"] == 10
        assert alert.metadata["current_volume"] == 1_000_000.0
        assert alert.metadata["avg_volume"] == 400_000.0

    def test_to_alert_rule(self):
        """to_alert_rule 应生成正确的 AlertRule。"""
        monitor = VolumeSpikeMonitor(
            "600519", lookback_days=20, spike_multiplier=2.5,
        )
        rule = monitor.to_alert_rule()
        assert rule.name == "VolumeSpike_600519"
        assert rule.stock_codes == ["600519"]
        assert rule.status == AlertStatus.ACTIVE
        assert rule.condition_value == "2.5"
        assert "20" in rule.description
        assert rule.severity == "warning"

    def test_constructor_rejects_invalid_lookback(self):
        """lookback_days < 1 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="lookback_days"):
            VolumeSpikeMonitor("600519", lookback_days=0)

    def test_constructor_rejects_invalid_multiplier(self):
        """spike_multiplier <= 0 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="spike_multiplier"):
            VolumeSpikeMonitor("600519", spike_multiplier=-1.0)


# ---------------------------------------------------------------------------
# PriceChangeMonitor 测试
# ---------------------------------------------------------------------------


class TestPriceChangeMonitor:
    """价格变动检测器测试。"""

    def test_price_up_triggers_when_exceeds_threshold(self):
        """涨幅超过阈值时应触发。"""
        monitor = PriceChangeMonitor("600519", change_threshold_pct=5.0)
        alert = monitor.check({
            "current_price": 1900.0,
            "prev_close": 1800.0,
        })
        assert alert is not None
        assert alert.triggered is True
        # (1900-1800)/1800*100 = 5.555...%
        assert alert.observed_value == pytest.approx(5.5556, abs=0.01)
        assert "涨" in alert.message

    def test_price_down_triggers_when_exceeds_threshold(self):
        """跌幅超过阈值时应触发。"""
        monitor = PriceChangeMonitor("600519", change_threshold_pct=5.0)
        alert = monitor.check({
            "current_price": 1700.0,
            "prev_close": 1800.0,
        })
        assert alert is not None
        assert alert.triggered is True
        # (1700-1800)/1800*100 = -5.555...%
        assert alert.observed_value == pytest.approx(-5.5556, abs=0.01)
        assert "跌" in alert.message

    def test_no_trigger_when_within_threshold(self):
        """变动在阈值内时不应触发。"""
        monitor = PriceChangeMonitor("600519", change_threshold_pct=5.0)
        alert = monitor.check({
            "current_price": 1820.0,
            "prev_close": 1800.0,
        })
        assert alert is not None
        assert alert.triggered is False
        assert abs(alert.observed_value) == pytest.approx(1.1111, abs=0.01)

    def test_price_unchanged(self):
        """价格不变时不应触发。"""
        monitor = PriceChangeMonitor("600519", change_threshold_pct=1.0)
        alert = monitor.check({
            "current_price": 1800.0,
            "prev_close": 1800.0,
        })
        assert alert is not None
        assert alert.triggered is False
        assert alert.observed_value == pytest.approx(0.0)

    def test_at_exact_threshold(self):
        """变动恰好等于阈值时应触发（>= 边界）。"""
        monitor = PriceChangeMonitor("600519", change_threshold_pct=5.0)
        # 1890 / 1800 = 1.05 → 5.0%
        alert = monitor.check({
            "current_price": 1890.0,
            "prev_close": 1800.0,
        })
        assert alert is not None
        assert alert.triggered is True
        assert alert.observed_value == pytest.approx(5.0)

    def test_missing_current_price_returns_none(self):
        """缺少 current_price 时返回 None。"""
        monitor = PriceChangeMonitor("600519")
        alert = monitor.check({"prev_close": 1800.0})
        assert alert is None

    def test_missing_prev_close_returns_none(self):
        """缺少 prev_close 时返回 None。"""
        monitor = PriceChangeMonitor("600519")
        alert = monitor.check({"current_price": 1900.0})
        assert alert is None

    def test_prev_close_zero_returns_none(self):
        """prev_close 为 0 时返回 None（避免除零）。"""
        monitor = PriceChangeMonitor("600519")
        alert = monitor.check({
            "current_price": 100.0,
            "prev_close": 0,
        })
        assert alert is None

    def test_prev_close_negative_returns_none(self):
        """prev_close 为负数时返回 None。"""
        monitor = PriceChangeMonitor("600519")
        alert = monitor.check({
            "current_price": 100.0,
            "prev_close": -10.0,
        })
        assert alert is None

    def test_empty_data_returns_none(self):
        """空数据字典返回 None。"""
        monitor = PriceChangeMonitor("600519")
        alert = monitor.check({})
        assert alert is None

    def test_metadata_contains_prices(self):
        """metadata 应包含价格信息。"""
        monitor = PriceChangeMonitor("600519")
        alert = monitor.check({
            "current_price": 1900.0,
            "prev_close": 1800.0,
        })
        assert alert is not None
        assert alert.metadata["current_price"] == 1900.0
        assert alert.metadata["prev_close"] == 1800.0
        assert "change_pct" in alert.metadata

    def test_to_alert_rule(self):
        """to_alert_rule 应生成正确的 AlertRule。"""
        monitor = PriceChangeMonitor("600519", change_threshold_pct=3.0)
        rule = monitor.to_alert_rule()
        assert rule.name == "PriceChange_600519"
        assert rule.stock_codes == ["600519"]
        assert rule.condition == AlertCondition.CHANGE_ABOVE
        assert rule.condition_value == "3.0"
        assert rule.status == AlertStatus.ACTIVE

    def test_constructor_rejects_invalid_threshold(self):
        """change_threshold_pct <= 0 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="change_threshold_pct"):
            PriceChangeMonitor("600519", change_threshold_pct=0)


# ---------------------------------------------------------------------------
# SentimentMonitor 测试
# ---------------------------------------------------------------------------


class TestSentimentMonitor:
    """情绪阈值检测器测试。"""

    def test_bullish_sentiment_triggers(self):
        """看多情绪超过阈值时应触发。"""
        monitor = SentimentMonitor("600519", sentiment_threshold=0.7)
        alert = monitor.check({"sentiment_score": 0.85})
        assert alert is not None
        assert alert.triggered is True
        assert alert.observed_value == pytest.approx(0.85)
        assert "看多" in alert.message

    def test_bearish_sentiment_triggers(self):
        """看空情绪超过阈值时应触发。"""
        monitor = SentimentMonitor("600519", sentiment_threshold=0.7)
        alert = monitor.check({"sentiment_score": -0.9})
        assert alert is not None
        assert alert.triggered is True
        assert alert.observed_value == pytest.approx(-0.9)
        assert "看空" in alert.message

    def test_neutral_sentiment_no_trigger(self):
        """中性情绪不应触发。"""
        monitor = SentimentMonitor("600519", sentiment_threshold=0.7)
        alert = monitor.check({"sentiment_score": 0.3})
        assert alert is not None
        assert alert.triggered is False
        assert "平稳" in alert.message

    def test_at_exact_threshold(self):
        """情绪恰好等于阈值时应触发（>= 边界）。"""
        monitor = SentimentMonitor("600519", sentiment_threshold=0.7)
        alert = monitor.check({"sentiment_score": 0.7})
        assert alert is not None
        assert alert.triggered is True

    def test_zero_sentiment_no_trigger(self):
        """零情绪不应触发。"""
        monitor = SentimentMonitor("600519", sentiment_threshold=0.5)
        alert = monitor.check({"sentiment_score": 0.0})
        assert alert is not None
        assert alert.triggered is False
        assert alert.observed_value == pytest.approx(0.0)

    def test_missing_sentiment_score_returns_none(self):
        """缺少 sentiment_score 时返回 None。"""
        monitor = SentimentMonitor("600519")
        alert = monitor.check({})
        assert alert is None

    def test_custom_severity(self):
        """自定义 severity 应正确传递。"""
        monitor = SentimentMonitor("600519", severity="error")
        alert = monitor.check({"sentiment_score": 0.9})
        assert alert is not None
        assert alert.severity == "error"

    def test_metadata_contains_direction(self):
        """metadata 应包含 direction 字段。"""
        monitor = SentimentMonitor("600519")
        alert = monitor.check({"sentiment_score": 0.8})
        assert alert is not None
        assert alert.metadata["direction"] == "看多"

        alert2 = monitor.check({"sentiment_score": -0.8})
        assert alert2 is not None
        assert alert2.metadata["direction"] == "看空"

    def test_to_alert_rule(self):
        """to_alert_rule 应生成正确的 AlertRule。"""
        monitor = SentimentMonitor("600519", sentiment_threshold=0.8)
        rule = monitor.to_alert_rule()
        assert rule.name == "Sentiment_600519"
        assert rule.stock_codes == ["600519"]
        assert rule.condition == AlertCondition.CHANGE_ABOVE
        assert rule.condition_value == "0.8"
        assert rule.status == AlertStatus.ACTIVE
        assert "情绪" in rule.description

    def test_constructor_rejects_invalid_threshold_zero(self):
        """sentiment_threshold <= 0 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="sentiment_threshold"):
            SentimentMonitor("600519", sentiment_threshold=0)

    def test_constructor_rejects_invalid_threshold_too_high(self):
        """sentiment_threshold > 2.0 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="sentiment_threshold"):
            SentimentMonitor("600519", sentiment_threshold=2.5)


# ---------------------------------------------------------------------------
# MonitorAlert 通用测试
# ---------------------------------------------------------------------------


class TestMonitorAlert:
    """MonitorAlert 数据结构测试。"""

    def test_alert_is_frozen(self):
        """MonitorAlert 应是不可变的（frozen dataclass）。"""
        alert = MonitorAlert(
            monitor_type=MonitorType.VOLUME_SPIKE,
            stock_code="600519",
            triggered=True,
            observed_value=2.5,
            threshold=2.0,
            message="test",
        )
        with pytest.raises(AttributeError):
            alert.triggered = False  # type: ignore[misc]

    def test_to_dict(self):
        """to_dict 应返回完整字典。"""
        alert = MonitorAlert(
            monitor_type=MonitorType.PRICE_CHANGE,
            stock_code="600519",
            triggered=True,
            observed_value=5.5,
            threshold=5.0,
            message="涨 5.5%",
            severity="error",
            metadata={"current_price": 1900.0},
        )
        d = alert.to_dict()
        assert d["monitor_type"] == "price_change"
        assert d["stock_code"] == "600519"
        assert d["triggered"] is True
        assert d["observed_value"] == 5.5
        assert d["severity"] == "error"
        assert d["metadata"]["current_price"] == 1900.0

    def test_default_severity_is_warning(self):
        """默认 severity 应为 warning。"""
        alert = MonitorAlert(
            monitor_type=MonitorType.SENTIMENT,
            stock_code="600519",
            triggered=False,
            observed_value=0.3,
            threshold=0.7,
            message="test",
        )
        assert alert.severity == "warning"

    def test_default_metadata_is_empty_dict(self):
        """默认 metadata 应为空字典。"""
        alert = MonitorAlert(
            monitor_type=MonitorType.VOLUME_SPIKE,
            stock_code="600519",
            triggered=False,
            observed_value=1.0,
            threshold=2.0,
            message="test",
        )
        assert alert.metadata == {}


# ---------------------------------------------------------------------------
# BaseMonitor 抽象测试
# ---------------------------------------------------------------------------


class TestBaseMonitorAbstract:
    """BaseMonitor 抽象类测试。"""

    def test_cannot_instantiate_base_monitor(self):
        """BaseMonitor 不能直接实例化。"""
        with pytest.raises(TypeError):
            BaseMonitor()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 集成测试: Monitor -> AlertRule -> AlertService 兼容性
# ---------------------------------------------------------------------------


class TestMonitorAlertRuleIntegration:
    """验证 Monitor 生成的 AlertRule 可被 AlertService 接受。"""

    def test_volume_spike_rule_compatible_with_alert_service(self):
        """VolumeSpikeMonitor 生成的 AlertRule 应可被 AlertService 使用。"""
        monitor = VolumeSpikeMonitor("600519", spike_multiplier=2.0)
        rule = monitor.to_alert_rule()

        # 验证 AlertRule 结构完整性
        assert rule.id  # 非空
        assert rule.name
        assert rule.stock_codes == ["600519"]
        assert rule.condition in (
            AlertCondition.CHANGE_ABOVE,
            AlertCondition.CHANGE_BELOW,
            AlertCondition.PRICE_ABOVE,
            AlertCondition.PRICE_BELOW,
        )
        assert rule.status == AlertStatus.ACTIVE
        assert rule.severity in ("info", "warning", "error", "critical")

    def test_price_change_rule_compatible_with_alert_service(self):
        """PriceChangeMonitor 生成的 AlertRule 应可被 AlertService 使用。"""
        monitor = PriceChangeMonitor("0700", change_threshold_pct=3.0)
        rule = monitor.to_alert_rule()

        assert rule.id
        assert rule.stock_codes == ["0700"]
        assert rule.condition_value == "3.0"

    def test_sentiment_rule_compatible_with_alert_service(self):
        """SentimentMonitor 生成的 AlertRule 应可被 AlertService 使用。"""
        monitor = SentimentMonitor("AAPL", sentiment_threshold=0.8)
        rule = monitor.to_alert_rule()

        assert rule.id
        assert rule.stock_codes == ["AAPL"]
        assert rule.condition_value == "0.8"
        assert "情绪" in rule.description
