# -*- coding: utf-8 -*-
"""Background worker for evaluating alert rules.

Designed to be called:
  1. After each analysis completes (post-analysis hook)
  2. Periodically by a scheduler (e.g. APScheduler or cron)

When a rule triggers and is not in cooldown, the worker sends notifications
via the configured channels (WeCom webhook, email).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from api.services.notification.alert_service import AlertRule, AlertService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AlertTriggerResult:
    """Result of evaluating a single alert rule."""
    rule_id: str
    symbol: str
    condition: str
    triggered: bool
    observed_value: Optional[float] = None
    threshold: Optional[float] = None
    message: str = ""
    notified: bool = False
    notification_error: Optional[str] = None


@dataclass
class WorkerStats:
    """Aggregate stats from one worker cycle."""
    loaded: int = 0
    evaluated: int = 0
    triggered: int = 0
    notified: int = 0
    cooldown_suppressed: int = 0
    errors: int = 0
    results: List[AlertTriggerResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class AlertWorker:
    """Evaluate all enabled alert rules and send notifications on trigger.

    Usage::

        worker = AlertWorker()
        stats = worker.run_once()
        # or for a specific symbol after analysis:
        stats = worker.run_for_symbol("AAPL", current_price=185.0)
    """

    def __init__(
        self,
        *,
        service: Optional[AlertService] = None,
        notifier: Optional[Callable[[AlertTriggerResult], bool]] = None,
    ) -> None:
        self.service = service or AlertService()
        # Custom notifier callback; if None, uses built-in WeCom/email
        self._custom_notifier = notifier

    # ── Public API ──────────────────────────────────────────────────────

    def run_once(self) -> WorkerStats:
        """Evaluate all enabled alert rules across all symbols.

        This is the main entry point for periodic background checks.
        Returns aggregate stats.
        """
        stats = WorkerStats()
        rules = self.service.list_all_enabled()
        stats.loaded = len(rules)

        if not rules:
            logger.debug("[AlertWorker] No active alert rules")
            return stats

        # Group rules by symbol for efficient data fetching
        by_symbol: Dict[str, List[AlertRule]] = {}
        for rule in rules:
            by_symbol.setdefault(rule.symbol, []).append(rule)

        for symbol, symbol_rules in by_symbol.items():
            market_data = self._fetch_market_data(symbol)
            for rule in symbol_rules:
                stats.evaluated += 1
                try:
                    result = self._evaluate_and_notify(rule, market_data)
                    stats.results.append(result)
                    if result.triggered:
                        stats.triggered += 1
                        if result.notified:
                            stats.notified += 1
                    if result.notification_error == "cooldown":
                        stats.cooldown_suppressed += 1
                except Exception as exc:
                    stats.errors += 1
                    logger.warning(
                        "[AlertWorker] Error evaluating rule %s for %s: %s",
                        rule.id, symbol, exc,
                    )

        logger.info(
            "[AlertWorker] Cycle complete: loaded=%d evaluated=%d triggered=%d "
            "notified=%d cooldown=%d errors=%d",
            stats.loaded, stats.evaluated, stats.triggered,
            stats.notified, stats.cooldown_suppressed, stats.errors,
        )
        return stats

    def run_for_symbol(
        self,
        symbol: str,
        *,
        current_price: Optional[float] = None,
        current_volume: Optional[float] = None,
        avg_volume: Optional[float] = None,
        sentiment_score: Optional[float] = None,
    ) -> WorkerStats:
        """Evaluate alert rules for a specific symbol.

        Called post-analysis with known market data.
        """
        stats = WorkerStats()
        rules = self.service.list_all_enabled(symbol=symbol)
        stats.loaded = len(rules)

        market_data = {
            "current_price": current_price,
            "current_volume": current_volume,
            "avg_volume": avg_volume,
            "sentiment_score": sentiment_score,
        }

        for rule in rules:
            stats.evaluated += 1
            try:
                result = self._evaluate_and_notify(rule, market_data)
                stats.results.append(result)
                if result.triggered:
                    stats.triggered += 1
                    if result.notified:
                        stats.notified += 1
                if result.notification_error == "cooldown":
                    stats.cooldown_suppressed += 1
            except Exception as exc:
                stats.errors += 1
                logger.warning(
                    "[AlertWorker] Error evaluating rule %s for %s: %s",
                    rule.id, symbol, exc,
                )

        return stats

    # ── Evaluation ──────────────────────────────────────────────────────

    def _evaluate_and_notify(
        self,
        rule: AlertRule,
        market_data: Dict[str, Any],
    ) -> AlertTriggerResult:
        """Evaluate a single rule and send notification if triggered."""
        eval_result = self.service.evaluate_rule(
            rule,
            current_price=market_data.get("current_price"),
            current_volume=market_data.get("current_volume"),
            avg_volume=market_data.get("avg_volume"),
            sentiment_score=market_data.get("sentiment_score"),
        )

        result = AlertTriggerResult(
            rule_id=rule.id,
            symbol=rule.symbol,
            condition=rule.condition,
            triggered=eval_result["triggered"],
            observed_value=eval_result.get("observed_value"),
            threshold=eval_result.get("threshold"),
            message=eval_result.get("message", ""),
        )

        if not result.triggered:
            return result

        # Check cooldown
        if self.service.check_cooldown(rule):
            result.notification_error = "cooldown"
            logger.debug("[AlertWorker] Rule %s in cooldown, skipping notification", rule.id)
            return result

        # Record trigger
        self.service.record_trigger(rule.id, result.observed_value)

        # Send notification
        try:
            notified = self._send_notification(rule, result)
            result.notified = notified
        except Exception as exc:
            result.notification_error = str(exc)
            logger.warning("[AlertWorker] Notification failed for rule %s: %s", rule.id, exc)

        return result

    def _send_notification(self, rule: AlertRule, result: AlertTriggerResult) -> bool:
        """Send notification for a triggered alert via unified dispatch."""
        if self._custom_notifier:
            return self._custom_notifier(result)

        title = f"⚠️ 交易预警 | {rule.symbol}"
        content = (
            f"{result.message}\n"
            f"条件: {rule.condition} (阈值: {rule.threshold})\n"
            f"当前值: {result.observed_value}\n"
            f"触发时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

        try:
            from api.services.notification_bridge import dispatch_notification
            dispatch_result = dispatch_notification(
                content,
                title=title,
                route_type="alert",
                severity="warning",
                user_id=str(rule.user_id),
            )
            if dispatch_result.get("sent"):
                logger.info("[AlertWorker] Alert notification sent for %s via %s", rule.symbol, dispatch_result.get("channels"))
                return True
            else:
                logger.info("[AlertWorker] Alert notification failed for %s: %s", rule.symbol, dispatch_result.get("message"))
                return False
        except Exception as exc:
            logger.warning("[AlertWorker] Notification failed for rule %s: %s", rule.id, exc)
            return False

    # ── Market data fetching ────────────────────────────────────────────

    @staticmethod
    def _fetch_market_data(symbol: str) -> Dict[str, Any]:
        """Fetch current market data for a symbol from Futu OpenD.

        Returns a dict with current_price, current_volume, avg_volume, etc.
        Falls back gracefully if data is unavailable.
        """
        data: Dict[str, Any] = {
            "current_price": None,
            "current_volume": None,
            "avg_volume": None,
            "sentiment_score": None,
        }

        # Try Futu OpenD for real-time quote
        try:
            from futu import OpenQuoteContext, RET_OK
            import os
            futu_host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
            futu_port = int(os.getenv("FUTU_OPEND_PORT", "11111"))

            with OpenQuoteContext(host=futu_host, port=futu_port) as quote_ctx:
                # Determine market code
                code = _to_futu_code(symbol)
                if code:
                    ret, quote_df = quote_ctx.get_market_snapshot([code])
                    if ret == RET_OK and not quote_df.empty:
                        row = quote_df.iloc[0]
                        data["current_price"] = float(row.get("last_price", 0) or 0)
                        data["current_volume"] = float(row.get("volume", 0) or 0)

                    # Get recent K-line for average volume
                    ret, kline_df = quote_ctx.request_history_kline(
                        code, ktype="K_DAY", count=20,
                    )
                    if ret == RET_OK and not kline_df.empty and "volume" in kline_df:
                        data["avg_volume"] = float(kline_df["volume"].mean())
        except ImportError:
            logger.debug("[AlertWorker] futu-api not installed, skipping real-time data")
        except Exception as exc:
            logger.warning("[AlertWorker] Failed to fetch market data for %s: %s", symbol, exc)

        return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_futu_code(symbol: str) -> Optional[str]:
    """Convert a symbol string to Futu market code (e.g. 'AAPL' -> 'US.AAPL').

    Uses stock_resolver (35k universe) as primary lookup, with heuristic fallback.
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return None
    # Already a Futu code
    if "." in s and s.split(".")[0] in ("US", "HK", "SH", "SZ"):
        return s
    # Try stock resolver first (covers US/ETF/HK)
    try:
        from tradingagents.dataflows.stock_resolver import to_futu
        resolved = to_futu(s)
        if resolved and "." in resolved:
            return resolved
    except ImportError:
        pass
    # Heuristic fallback: 6-digit = HK, otherwise US
    if s.isdigit() and len(s) == 6:
        if s.startswith("6"):
            return f"SH.{s}"
        return f"SZ.{s}"
    if s.startswith("HK") or (s.isdigit() and len(s) <= 5):
        return f"HK.{s.lstrip('HK')}"
    return f"US.{s}"
