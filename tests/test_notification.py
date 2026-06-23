# -*- coding: utf-8 -*-
"""
Phase 11 通知系统测试

覆盖：
- NoiseFilter: dedup / cooldown / quiet-hours
- AlertService: CRUD + evaluate + singleton + atomic write
- NotificationService: send + route
- Diagnostics: 配置健康检查
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# NoiseFilter 测试
# ---------------------------------------------------------------------------

from tradingagents.notification.noise import (
    NotificationNoiseDecision,
    evaluate_notification_noise,
    is_time_in_quiet_hours,
    normalize_notification_severity,
    parse_notification_quiet_hours,
    record_notification_noise,
    release_notification_noise,
    reset_notification_noise_state,
)


class _FakeConfig:
    """模拟配置对象。"""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestNoiseDedup:
    """去重（Dedup）测试。"""

    def setup_method(self):
        reset_notification_noise_state()

    def test_dedup_blocks_duplicate_within_ttl(self):
        """同一内容在 TTL 内应被去重拦截。"""
        config = _FakeConfig(notification_dedup_ttl_seconds=60)

        # 第一次：应允许
        d1 = evaluate_notification_noise(
            config,
            content="test message",
            route_type="alert",
            now=datetime(2026, 1, 1, 12, 0, 0),
        )
        assert d1.should_send is True

        # 记录发送成功
        record_notification_noise(d1, now=datetime(2026, 1, 1, 12, 0, 1))

        # 第二次（同内容，TTL 内）：应拦截
        d2 = evaluate_notification_noise(
            config,
            content="test message",
            route_type="alert",
            now=datetime(2026, 1, 1, 12, 0, 30),
        )
        assert d2.should_send is False
        assert d2.reason_code == "dedup"

    def test_dedup_allows_after_ttl_expires(self):
        """TTL 过期后同一内容应被允许发送。"""
        config = _FakeConfig(notification_dedup_ttl_seconds=60)

        d1 = evaluate_notification_noise(
            config,
            content="test message",
            route_type="alert",
            now=datetime(2026, 1, 1, 12, 0, 0),
        )
        record_notification_noise(d1, now=datetime(2026, 1, 1, 12, 0, 1))

        # TTL 后
        d2 = evaluate_notification_noise(
            config,
            content="test message",
            route_type="alert",
            now=datetime(2026, 1, 1, 12, 1, 2),
        )
        assert d2.should_send is True

    def test_dedup_different_content_not_blocked(self):
        """不同内容不应被去重拦截。"""
        config = _FakeConfig(notification_dedup_ttl_seconds=60)

        d1 = evaluate_notification_noise(
            config,
            content="message A",
            route_type="alert",
            now=datetime(2026, 1, 1, 12, 0, 0),
        )
        record_notification_noise(d1, now=datetime(2026, 1, 1, 12, 0, 1))

        d2 = evaluate_notification_noise(
            config,
            content="message B",
            route_type="alert",
            now=datetime(2026, 1, 1, 12, 0, 5),
        )
        assert d2.should_send is True


class TestNoiseCooldown:
    """冷却（Cooldown）测试。"""

    def setup_method(self):
        reset_notification_noise_state()

    def test_cooldown_blocks_within_window(self):
        """同一 cooldown_key 在冷却时间内应被拦截。"""
        config = _FakeConfig(notification_cooldown_seconds=120)

        d1 = evaluate_notification_noise(
            config,
            content="any content",
            route_type="alert",
            cooldown_key="stock_600519",
            now=datetime(2026, 1, 1, 12, 0, 0),
        )
        assert d1.should_send is True
        record_notification_noise(d1, now=datetime(2026, 1, 1, 12, 0, 1))

        d2 = evaluate_notification_noise(
            config,
            content="different content",
            route_type="alert",
            cooldown_key="stock_600519",
            now=datetime(2026, 1, 1, 12, 1, 0),
        )
        assert d2.should_send is False
        assert d2.reason_code == "cooldown"

    def test_cooldown_allows_after_window(self):
        """冷却时间过后应允许发送。"""
        config = _FakeConfig(notification_cooldown_seconds=120)

        d1 = evaluate_notification_noise(
            config,
            content="any content",
            route_type="alert",
            cooldown_key="stock_600519",
            now=datetime(2026, 1, 1, 12, 0, 0),
        )
        record_notification_noise(d1, now=datetime(2026, 1, 1, 12, 0, 1))

        d2 = evaluate_notification_noise(
            config,
            content="different content",
            route_type="alert",
            cooldown_key="stock_600519",
            now=datetime(2026, 1, 1, 12, 2, 2),
        )
        assert d2.should_send is True


class TestNoiseQuietHours:
    """静默时段（Quiet Hours）测试。"""

    def test_quiet_hours_blocks_during_window(self):
        """静默时段内的通知应被拦截。"""
        config = _FakeConfig(notification_quiet_hours="22:00-06:00")

        d = evaluate_notification_noise(
            config,
            content="night alert",
            route_type="alert",
            now=datetime(2026, 1, 1, 23, 30, 0),
        )
        assert d.should_send is False
        assert d.reason_code == "quiet_hours"

    def test_quiet_hours_allows_outside_window(self):
        """静默时段外的通知应被允许。"""
        config = _FakeConfig(notification_quiet_hours="22:00-06:00")

        d = evaluate_notification_noise(
            config,
            content="daytime alert",
            route_type="alert",
            now=datetime(2026, 1, 1, 14, 0, 0),
        )
        assert d.should_send is True

    def test_quiet_hours_non_wrapping(self):
        """非跨午夜静默时段测试。"""
        config = _FakeConfig(notification_quiet_hours="12:00-14:00")

        # 在时段内
        d1 = evaluate_notification_noise(
            config, content="lunch", route_type="alert",
            now=datetime(2026, 1, 1, 13, 0, 0),
        )
        assert d1.should_send is False

        # 在时段外
        d2 = evaluate_notification_noise(
            config, content="morning", route_type="alert",
            now=datetime(2026, 1, 1, 9, 0, 0),
        )
        assert d2.should_send is True

    def test_parse_quiet_hours_valid(self):
        """解析有效静默时段。"""
        assert parse_notification_quiet_hours("22:00-06:00") == (1320, 360)
        assert parse_notification_quiet_hours("00:00-23:59") == (0, 1439)

    def test_parse_quiet_hours_invalid(self):
        """无效格式应抛出 ValueError。"""
        with pytest.raises(ValueError):
            parse_notification_quiet_hours("25:00-06:00")
        with pytest.raises(ValueError):
            parse_notification_quiet_hours("invalid")

    def test_is_time_in_quiet_hours(self):
        """时间判断函数测试。"""
        assert is_time_in_quiet_hours(datetime(2026, 1, 1, 23, 0), (1320, 360)) is True
        assert is_time_in_quiet_hours(datetime(2026, 1, 1, 3, 0), (1320, 360)) is True
        assert is_time_in_quiet_hours(datetime(2026, 1, 1, 12, 0), (1320, 360)) is False


class TestNoiseMinSeverity:
    """最低级别过滤测试。"""

    def setup_method(self):
        reset_notification_noise_state()

    def test_below_min_severity_blocked(self):
        """低于最低级别的通知应被拦截。"""
        config = _FakeConfig(notification_min_severity="warning")

        d = evaluate_notification_noise(
            config,
            content="info message",
            route_type="report",
            severity="info",
            now=datetime(2026, 1, 1, 12, 0, 0),
        )
        assert d.should_send is False
        assert d.reason_code == "min_severity"

    def test_above_min_severity_allowed(self):
        """等于或高于最低级别的通知应被允许。"""
        config = _FakeConfig(notification_min_severity="warning")

        d = evaluate_notification_noise(
            config,
            content="error message",
            route_type="alert",
            severity="error",
            now=datetime(2026, 1, 1, 12, 0, 0),
        )
        assert d.should_send is True


class TestNoiseSeverityNormalization:
    """严重级别归一化测试。"""

    def test_explicit_severity(self):
        assert normalize_notification_severity("alert", "critical") == "critical"

    def test_default_by_route(self):
        assert normalize_notification_severity("report") == "info"
        assert normalize_notification_severity("alert") == "warning"
        assert normalize_notification_severity("system_error") == "error"


# ---------------------------------------------------------------------------
# AlertService 测试
# ---------------------------------------------------------------------------

from tradingagents.notification.alert_service import (
    AlertCondition,
    AlertRule,
    AlertService,
    AlertStatus,
)


class TestAlertServiceCRUD:
    """AlertService CRUD 操作测试。"""

    def setup_method(self):
        AlertService._reset_singletons()

    def test_create_and_get_rule(self, tmp_path):
        """创建规则后应能通过 ID 获取。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        rule = AlertRule(name="test", condition=AlertCondition.SIGNAL_MATCH, condition_value="bullish")
        created = svc.create_rule(rule)

        assert created.id
        assert created.name == "test"
        assert svc.get_rule(created.id) is not None

    def test_list_rules_with_filter(self, tmp_path):
        """列出规则支持按状态过滤。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        svc.create_rule(AlertRule(name="active", status=AlertStatus.ACTIVE))
        svc.create_rule(AlertRule(name="paused", status=AlertStatus.PAUSED))

        all_rules = svc.list_rules()
        active_rules = svc.list_rules(status=AlertStatus.ACTIVE)
        assert len(all_rules) == 2
        assert len(active_rules) == 1

    def test_update_rule(self, tmp_path):
        """更新规则字段。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        rule = svc.create_rule(AlertRule(name="original"))
        updated = svc.update_rule(rule.id, name="updated")

        assert updated is not None
        assert updated.name == "updated"
        fetched = svc.get_rule(rule.id)
        assert fetched is not None
        assert fetched.name == "updated"

    def test_delete_rule(self, tmp_path):
        """删除规则后应无法获取。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        rule = svc.create_rule(AlertRule(name="to_delete"))

        assert svc.delete_rule(rule.id) is True
        assert svc.get_rule(rule.id) is None

    def test_delete_nonexistent_rule(self, tmp_path):
        """删除不存在的规则应返回 False。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        assert svc.delete_rule("nonexistent") is False


class TestAlertServiceEvaluate:
    """AlertService 规则评估测试。"""

    def setup_method(self):
        AlertService._reset_singletons()

    def test_evaluate_signal_match(self, tmp_path):
        """信号匹配条件测试。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        svc.create_rule(AlertRule(
            name="bullish",
            condition=AlertCondition.SIGNAL_MATCH,
            condition_value="bullish",
        ))

        matched = svc.evaluate("600519", {"signal": "bullish"})
        assert len(matched) == 1
        assert matched[0].name == "bullish"

    def test_evaluate_price_above(self, tmp_path):
        """价格高于阈值条件测试。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        svc.create_rule(AlertRule(
            name="high_price",
            condition=AlertCondition.PRICE_ABOVE,
            condition_value="100",
        ))

        matched = svc.evaluate("600519", {"price": 150})
        assert len(matched) == 1

        not_matched = svc.evaluate("600519", {"price": 50})
        assert len(not_matched) == 0

    def test_evaluate_stock_filter(self, tmp_path):
        """股票代码过滤测试。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        svc.create_rule(AlertRule(
            name="specific_stock",
            stock_codes=["600519"],
            condition=AlertCondition.ANY_ANALYSIS,
        ))

        matched = svc.evaluate("600519", {})
        assert len(matched) == 1

        not_matched = svc.evaluate("000001", {})
        assert len(not_matched) == 0


class TestAlertServiceSingleton:
    """AlertService 单例模式测试（B4 修复验证）。"""

    def setup_method(self):
        AlertService._reset_singletons()

    def test_same_path_returns_same_instance(self, tmp_path):
        """同一 path 应返回同一实例。"""
        path = str(tmp_path / "rules.json")
        svc1 = AlertService(path)
        svc2 = AlertService(path)
        assert svc1 is svc2

    def test_different_path_returns_different_instance(self, tmp_path):
        """不同 path 应返回不同实例。"""
        svc1 = AlertService(str(tmp_path / "rules1.json"))
        svc2 = AlertService(str(tmp_path / "rules2.json"))
        assert svc1 is not svc2

    def test_thread_safety(self, tmp_path):
        """多线程并发创建应返回同一实例。"""
        path = str(tmp_path / "rules.json")
        instances = []
        barrier = threading.Barrier(10)

        def create_instance():
            barrier.wait(timeout=5)
            instances.append(AlertService(path))

        threads = [threading.Thread(target=create_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(inst is instances[0] for inst in instances)


class TestAlertServiceAtomicWrite:
    """AlertService 原子写入测试（B5 修复验证）。"""

    def setup_method(self):
        AlertService._reset_singletons()

    def test_rules_persist_after_save(self, tmp_path):
        """保存后规则应持久化到文件。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        svc.create_rule(AlertRule(name="persist_test"))

        # 重新加载
        AlertService._reset_singletons()
        svc2 = AlertService(path)
        rules = svc2.list_rules()
        assert len(rules) == 1
        assert rules[0].name == "persist_test"

    def test_no_temp_files_left_after_save(self, tmp_path):
        """保存后不应残留临时文件。"""
        path = str(tmp_path / "rules.json")
        svc = AlertService(path)
        svc.create_rule(AlertRule(name="clean_test"))

        tmp_files = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
        assert len(tmp_files) == 0


# ---------------------------------------------------------------------------
# AlertRule 数据结构测试
# ---------------------------------------------------------------------------

class TestAlertRule:
    """AlertRule 数据结构测试。"""

    def test_matches_stock_empty_means_all(self):
        """空 stock_codes 应匹配所有股票。"""
        rule = AlertRule(stock_codes=[])
        assert rule.matches_stock("600519") is True

    def test_matches_stock_specific(self):
        """指定 stock_codes 应只匹配列表中的股票。"""
        rule = AlertRule(stock_codes=["600519", "000001"])
        assert rule.matches_stock("600519") is True
        assert rule.matches_stock("999999") is False

    def test_matches_condition_signal(self):
        """信号匹配条件测试。"""
        rule = AlertRule(condition=AlertCondition.SIGNAL_MATCH, condition_value="bullish")
        assert rule.matches_condition({"signal": "bullish"}) is True
        assert rule.matches_condition({"signal": "bearish"}) is False

    def test_matches_condition_price_above(self):
        """价格高于阈值条件测试。"""
        rule = AlertRule(condition=AlertCondition.PRICE_ABOVE, condition_value="100")
        assert rule.matches_condition({"price": 150}) is True
        assert rule.matches_condition({"price": 50}) is False
        assert rule.matches_condition({}) is False

    def test_matches_condition_any_analysis(self):
        """任意分析条件应始终匹配。"""
        rule = AlertRule(condition=AlertCondition.ANY_ANALYSIS)
        assert rule.matches_condition({}) is True

    def test_from_dict_roundtrip(self):
        """序列化/反序列化往返测试。"""
        rule = AlertRule(name="test", condition=AlertCondition.PRICE_ABOVE, condition_value="100")
        d = rule.to_dict()
        restored = AlertRule.from_dict(d)
        assert restored.name == "test"
        assert restored.condition == AlertCondition.PRICE_ABOVE


# ---------------------------------------------------------------------------
# NotificationService 测试
# ---------------------------------------------------------------------------

from tradingagents.notification.core import (
    NotificationChannel,
    NotificationService,
    Sender,
)


class _MockSender(Sender):
    """模拟发送器，记录发送调用。"""

    channel = NotificationChannel.TELEGRAM

    def __init__(self, config=None, configured=True):
        super().__init__(config or {})
        self._configured = configured
        self.send_calls = []

    def is_configured(self):
        return self._configured

    def send(self, content, *, title=None, timeout_seconds=None, image_bytes=None):
        self.send_calls.append({"content": content, "title": title})
        return True


class TestNotificationServiceSend:
    """NotificationService 发送测试。"""

    def test_send_to_registered_channel(self):
        """已注册渠道应能发送消息。"""
        svc = NotificationService()
        sender = _MockSender()
        svc.register_sender(sender)

        result = svc.send("test message", skip_noise_check=True)
        assert result.success is True
        assert result.dispatched is True
        assert len(sender.send_calls) == 1

    def test_send_no_channels(self):
        """无可用渠道应返回 no_channels 状态。"""
        svc = NotificationService()
        result = svc.send("test message", skip_noise_check=True)
        assert result.success is False
        assert result.status == "no_channels"

    def test_send_with_route_type(self):
        """路由类型应决定目标渠道。"""
        svc = NotificationService(config={"notification_alert_channels": ["telegram"]})
        sender = _MockSender()
        svc.register_sender(sender)

        result = svc.send("alert", route_type="alert", skip_noise_check=True)
        assert result.success is True


class TestNotificationServiceRouting:
    """NotificationService 路由测试。"""

    def test_explicit_target_channels(self):
        """显式指定渠道应覆盖路由配置。"""
        svc = NotificationService()
        sender = _MockSender()
        svc.register_sender(sender)

        result = svc.send("test", target_channels=["telegram"], skip_noise_check=True)
        assert result.success is True

    def test_unregistered_channel_in_target(self):
        """未注册的渠道应被跳过。"""
        svc = NotificationService()
        sender = _MockSender()
        svc.register_sender(sender)

        result = svc.send("test", target_channels=["email"], skip_noise_check=True)
        # email 未注册，但 telegram 已注册 — 无匹配渠道
        assert result.success is False


class TestNotificationServiceDetection:
    """NotificationService 渠道检测测试。"""

    def test_detect_telegram(self):
        """应检测到 Telegram 配置。"""
        config = {"telegram_bot_token": "xxx", "telegram_chat_id": "123"}
        detected = NotificationService.detect_configured_channels(config)
        assert NotificationChannel.TELEGRAM in detected

    def test_detect_feishu(self):
        """应检测到飞书配置。"""
        config = {"feishu_webhook_url": "https://open.feishu.cn/xxx"}
        detected = NotificationService.detect_configured_channels(config)
        assert NotificationChannel.FEISHU in detected

    def test_detect_no_channels(self):
        """空配置应检测到 0 个渠道。"""
        detected = NotificationService.detect_configured_channels({})
        assert len(detected) == 0


# ---------------------------------------------------------------------------
# Sender 脱敏测试（B2 修复验证）
# ---------------------------------------------------------------------------

class TestSenderMaskToken:
    """Sender._mask_token 脱敏测试。"""

    def test_mask_long_token(self):
        """长 token 应只保留前 6 位。"""
        assert Sender._mask_token("abcdefghijklmnop") == "abcdef***"

    def test_mask_short_token(self):
        """短 token 应完全遮蔽。"""
        assert Sender._mask_token("abc") == "***"

    def test_mask_empty(self):
        """空字符串应返回 ***。"""
        assert Sender._mask_token("") == "***"

    def test_mask_custom_keep(self):
        """自定义保留长度。"""
        assert Sender._mask_token("abcdefghijklmnop", keep=3) == "abc***"


# ---------------------------------------------------------------------------
# Diagnostics 测试
# ---------------------------------------------------------------------------

from tradingagents.notification.diagnostics import (
    format_notification_diagnostics,
    run_notification_diagnostics,
)


class TestDiagnostics:
    """通知配置诊断测试。"""

    def test_no_channels_configured(self):
        """无渠道配置应报错。"""
        result = run_notification_diagnostics({})
        assert result.ok is False
        error_codes = [e.code for e in result.errors]
        assert "no_channels_configured" in error_codes

    def test_telegram_fully_configured(self):
        """Telegram 完整配置应无错误。"""
        config = {
            "TELEGRAM_BOT_TOKEN": "xxx",
            "TELEGRAM_CHAT_ID": "123",
        }
        result = run_notification_diagnostics(config)
        assert result.ok is True
        assert "telegram" in result.configured_channels

    def test_partial_telegram_config(self):
        """Telegram 部分配置应报错。"""
        config = {"TELEGRAM_BOT_TOKEN": "xxx"}
        result = run_notification_diagnostics(config)
        assert result.ok is False
        error_codes = [e.code for e in result.errors]
        assert "partial_channel_config" in error_codes

    def test_invalid_quiet_hours(self):
        """无效静默时段配置应报错。"""
        config = {
            "TELEGRAM_BOT_TOKEN": "xxx",
            "TELEGRAM_CHAT_ID": "123",
            "NOTIFICATION_QUIET_HOURS": "invalid",
        }
        result = run_notification_diagnostics(config)
        assert result.ok is False
        error_codes = [e.code for e in result.errors]
        assert "invalid_quiet_hours" in error_codes

    def test_format_diagnostics(self):
        """格式化输出应包含诊断信息。"""
        result = run_notification_diagnostics({})
        text = format_notification_diagnostics(result)
        assert "通知配置诊断" in text
        assert "Errors" in text
