# -*- coding: utf-8 -*-
"""Alert rule CRUD and evaluation service.

Stores user-defined alert rules in SQLite and evaluates them against
real-time market data from Futu OpenD or daily analysis results.
"""

from __future__ import annotations

import enum
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, Column, DateTime, Float, String, Text, Integer, JSON
from sqlalchemy.orm import Session

from api.database import Base, get_db_ctx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQLAlchemy model
# ---------------------------------------------------------------------------

class AlertRule(Base):
    """Persisted alert rule."""

    __tablename__ = "alert_rules"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    symbol = Column(String(20), nullable=False, index=True)

    # One of: price_above, price_below, volume_spike, sentiment_change
    condition = Column(String(32), nullable=False)
    threshold = Column(Float, nullable=False)

    # Optional metadata
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)

    # Cooldown: minimum seconds between repeated notifications
    cooldown_seconds = Column(Integer, default=3600, nullable=False)

    # Tracking
    last_triggered_at = Column(DateTime, nullable=True)
    last_observed_value = Column(Float, nullable=True)
    trigger_count = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "symbol": self.symbol,
            "condition": self.condition,
            "threshold": self.threshold,
            "description": self.description,
            "enabled": self.enabled,
            "cooldown_seconds": self.cooldown_seconds,
            "last_triggered_at": (
                self.last_triggered_at.isoformat() if self.last_triggered_at else None
            ),
            "last_observed_value": self.last_observed_value,
            "trigger_count": self.trigger_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ---------------------------------------------------------------------------
# Enums & constants
# ---------------------------------------------------------------------------

class AlertCondition(str, enum.Enum):
    """Supported alert condition types."""
    PRICE_ABOVE = "price_above"
    PRICE_BELOW = "price_below"
    VOLUME_SPIKE = "volume_spike"
    SENTIMENT_CHANGE = "sentiment_change"


SUPPORTED_CONDITIONS = frozenset(c.value for c in AlertCondition)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class AlertServiceError(ValueError):
    """Raised when alert service input is invalid."""


class AlertNotFoundError(AlertServiceError):
    """Raised when an alert resource does not exist."""


class AlertService:
    """Business logic for alert rule CRUD and evaluation."""

    # ── CRUD ────────────────────────────────────────────────────────────

    def create_rule(
        self,
        user_id: str,
        symbol: str,
        condition: str,
        threshold: float,
        *,
        description: str = "",
        cooldown_seconds: int = 3600,
    ) -> dict:
        """Create a new alert rule.

        Returns the serialized rule dict.
        """
        self._validate_condition(condition)
        symbol = self._normalize_symbol(symbol)

        from uuid import uuid4
        rule = AlertRule(
            id=str(uuid4()),
            user_id=user_id,
            symbol=symbol,
            condition=condition,
            threshold=float(threshold),
            description=description or "",
            cooldown_seconds=max(60, int(cooldown_seconds)),
        )
        with get_db_ctx() as db:
            db.add(rule)
            db.commit()
            db.refresh(rule)
            logger.info("[AlertService] Created rule %s for %s %s", rule.id, symbol, condition)
            return rule.to_dict()

    def get_rule(self, rule_id: str, user_id: Optional[str] = None) -> dict:
        """Get a single alert rule by ID."""
        with get_db_ctx() as db:
            rule = self._fetch_rule(db, rule_id, user_id)
            return rule.to_dict()

    def update_rule(self, rule_id: str, user_id: str, **fields) -> dict:
        """Update an existing alert rule.

        Allowed fields: threshold, description, enabled, cooldown_seconds, condition.
        """
        allowed = {"threshold", "description", "enabled", "cooldown_seconds", "condition"}
        invalid = set(fields.keys()) - allowed
        if invalid:
            raise AlertServiceError(f"Cannot update fields: {invalid}")

        if "condition" in fields:
            self._validate_condition(fields["condition"])

        with get_db_ctx() as db:
            rule = self._fetch_rule(db, rule_id, user_id)
            for key, value in fields.items():
                if key == "threshold":
                    value = float(value)
                elif key == "cooldown_seconds":
                    value = max(60, int(value))
                setattr(rule, key, value)
            db.commit()
            db.refresh(rule)
            logger.info("[AlertService] Updated rule %s", rule_id)
            return rule.to_dict()

    def delete_rule(self, rule_id: str, user_id: str) -> bool:
        """Delete an alert rule. Returns True if deleted."""
        with get_db_ctx() as db:
            rule = self._fetch_rule(db, rule_id, user_id)
            db.delete(rule)
            db.commit()
            logger.info("[AlertService] Deleted rule %s", rule_id)
            return True

    def list_rules(
        self,
        user_id: str,
        *,
        symbol: Optional[str] = None,
        condition: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> List[dict]:
        """List alert rules for a user with optional filters."""
        with get_db_ctx() as db:
            q = db.query(AlertRule).filter(AlertRule.user_id == user_id)
            if symbol:
                q = q.filter(AlertRule.symbol == self._normalize_symbol(symbol))
            if condition:
                q = q.filter(AlertRule.condition == condition)
            if enabled is not None:
                q = q.filter(AlertRule.enabled == enabled)
            q = q.order_by(AlertRule.created_at.desc())
            return [r.to_dict() for r in q.all()]

    def list_all_enabled(self, symbol: Optional[str] = None) -> List[AlertRule]:
        """List all enabled rules (across users), optionally filtered by symbol.

        Used by AlertWorker for batch evaluation.
        """
        with get_db_ctx() as db:
            q = db.query(AlertRule).filter(AlertRule.enabled == True)  # noqa: E712
            if symbol:
                q = q.filter(AlertRule.symbol == self._normalize_symbol(symbol))
            return q.all()

    # ── Evaluation ──────────────────────────────────────────────────────

    def evaluate_rule(
        self,
        rule: AlertRule,
        current_price: Optional[float] = None,
        current_volume: Optional[float] = None,
        avg_volume: Optional[float] = None,
        sentiment_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Evaluate a single rule against current market data.

        Returns a dict with keys: triggered, observed_value, threshold, message.
        """
        condition = rule.condition
        threshold = rule.threshold

        if condition == AlertCondition.PRICE_ABOVE.value:
            if current_price is None:
                return self._not_triggered(rule, None, "No price data available")
            triggered = current_price >= threshold
            msg = (
                f"{rule.symbol} price {current_price:.2f} >= {threshold:.2f}"
                if triggered
                else f"{rule.symbol} price {current_price:.2f} < {threshold:.2f}"
            )
            return self._result(rule, triggered, current_price, msg)

        elif condition == AlertCondition.PRICE_BELOW.value:
            if current_price is None:
                return self._not_triggered(rule, None, "No price data available")
            triggered = current_price <= threshold
            msg = (
                f"{rule.symbol} price {current_price:.2f} <= {threshold:.2f}"
                if triggered
                else f"{rule.symbol} price {current_price:.2f} > {threshold:.2f}"
            )
            return self._result(rule, triggered, current_price, msg)

        elif condition == AlertCondition.VOLUME_SPIKE.value:
            if current_volume is None or avg_volume is None or avg_volume <= 0:
                return self._not_triggered(rule, None, "No volume data available")
            ratio = current_volume / avg_volume
            triggered = ratio >= threshold
            msg = (
                f"{rule.symbol} volume spike {ratio:.1f}x >= {threshold:.1f}x"
                if triggered
                else f"{rule.symbol} volume ratio {ratio:.1f}x < {threshold:.1f}x"
            )
            return self._result(rule, triggered, ratio, msg)

        elif condition == AlertCondition.SENTIMENT_CHANGE.value:
            if sentiment_score is None:
                return self._not_triggered(rule, None, "No sentiment data available")
            triggered = abs(sentiment_score) >= threshold
            msg = (
                f"{rule.symbol} sentiment change {sentiment_score:+.2f} >= ±{threshold:.2f}"
                if triggered
                else f"{rule.symbol} sentiment {sentiment_score:+.2f} within ±{threshold:.2f}"
            )
            return self._result(rule, triggered, sentiment_score, msg)

        else:
            return self._not_triggered(rule, None, f"Unknown condition: {condition}")

    def check_cooldown(self, rule: AlertRule) -> bool:
        """Return True if the rule is still in cooldown (should suppress notification)."""
        if rule.last_triggered_at is None:
            return False
        elapsed = (datetime.now(timezone.utc) - rule.last_triggered_at).total_seconds()
        return elapsed < rule.cooldown_seconds

    def record_trigger(
        self,
        rule_id: str,
        observed_value: Optional[float] = None,
    ) -> None:
        """Record that a rule was triggered (update tracking fields)."""
        with get_db_ctx() as db:
            rule = db.query(AlertRule).filter(AlertRule.id == rule_id).first()
            if rule is None:
                return
            rule.last_triggered_at = datetime.now(timezone.utc)
            rule.last_observed_value = observed_value
            rule.trigger_count = (rule.trigger_count or 0) + 1
            db.commit()

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _validate_condition(condition: str) -> None:
        if condition not in SUPPORTED_CONDITIONS:
            raise AlertServiceError(
                f"Unsupported condition '{condition}'. "
                f"Must be one of: {', '.join(sorted(SUPPORTED_CONDITIONS))}"
            )

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """Normalize symbol to uppercase, strip whitespace."""
        return str(symbol or "").strip().upper()

    @staticmethod
    def _fetch_rule(db: Session, rule_id: str, user_id: Optional[str] = None) -> AlertRule:
        """Fetch a rule by ID, optionally verifying ownership."""
        q = db.query(AlertRule).filter(AlertRule.id == rule_id)
        if user_id is not None:
            q = q.filter(AlertRule.user_id == user_id)
        rule = q.first()
        if rule is None:
            raise AlertNotFoundError(f"Alert rule not found: {rule_id}")
        return rule

    @staticmethod
    def _result(
        rule: AlertRule,
        triggered: bool,
        observed_value: Optional[float],
        message: str,
    ) -> Dict[str, Any]:
        return {
            "rule_id": rule.id,
            "symbol": rule.symbol,
            "condition": rule.condition,
            "triggered": triggered,
            "observed_value": observed_value,
            "threshold": rule.threshold,
            "message": message,
        }

    @staticmethod
    def _not_triggered(
        rule: AlertRule,
        observed_value: Optional[float],
        message: str,
    ) -> Dict[str, Any]:
        return {
            "rule_id": rule.id,
            "symbol": rule.symbol,
            "condition": rule.condition,
            "triggered": False,
            "observed_value": observed_value,
            "threshold": rule.threshold,
            "message": message,
        }
