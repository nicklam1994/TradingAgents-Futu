# -*- coding: utf-8 -*-
"""Notification channel diagnostics.

Checks each notification channel (WeCom webhook, email SMTP) for
configuration completeness and connectivity.  Returns a structured
report suitable for display in a health-check endpoint or CLI.
"""

from __future__ import annotations

import logging
import os
import smtplib
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ChannelStatus:
    """Diagnostic result for a single notification channel."""
    channel: str
    configured: bool = False
    reachable: Optional[bool] = None  # None = not tested
    latency_ms: Optional[int] = None
    last_error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DiagnosticsReport:
    """Aggregate diagnostics report."""
    channels: List[ChannelStatus] = field(default_factory=list)
    timestamp: str = ""
    overall_healthy: bool = False

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "overall_healthy": self.overall_healthy,
            "channels": [
                {
                    "channel": ch.channel,
                    "configured": ch.configured,
                    "reachable": ch.reachable,
                    "latency_ms": ch.latency_ms,
                    "last_error": ch.last_error,
                    "details": ch.details,
                }
                for ch in self.channels
            ],
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_notification_diagnostics(
    *,
    test_connectivity: bool = True,
    user_id: Optional[str] = None,
) -> dict:
    """Run diagnostics on all configured notification channels.

    Args:
        test_connectivity: If True, attempt a lightweight connection test.
        user_id: If provided, also check user-specific channels (e.g. user WeCom webhook).

    Returns:
        A dict with overall status and per-channel details.
    """
    from datetime import datetime, timezone

    report = DiagnosticsReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    # 1. Server-level WeCom webhook
    report.channels.append(_check_wecom_server(test_connectivity))

    # 2. SMTP email
    report.channels.append(_check_smtp(test_connectivity))

    # 3. User-specific WeCom webhook (if user_id provided)
    if user_id:
        report.channels.append(_check_wecom_user(user_id, test_connectivity))

    # Overall health: at least one channel configured and reachable
    report.overall_healthy = any(
        ch.configured and ch.reachable is not False
        for ch in report.channels
    )

    return report.to_dict()


# ---------------------------------------------------------------------------
# Channel checkers
# ---------------------------------------------------------------------------

def _check_wecom_server(test_connectivity: bool) -> ChannelStatus:
    """Check server-level WeCom webhook (env var WECOM_WEBHOOK_URL)."""
    status = ChannelStatus(channel="wecom_server")
    webhook_url = os.getenv("WECOM_WEBHOOK_URL", "").strip()

    if not webhook_url:
        status.configured = False
        status.details["env_var"] = "WECOM_WEBHOOK_URL"
        status.details["hint"] = "Set WECOM_WEBHOOK_URL environment variable"
        return status

    status.configured = True
    status.details["url_masked"] = _mask_url(webhook_url)

    if not test_connectivity:
        return status

    # Test connectivity with a HEAD-like check (resolve host)
    try:
        from urllib.parse import urlparse
        parsed = urlparse(webhook_url)
        host = parsed.hostname or ""
        port = parsed.port or 443

        start = time.monotonic()
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        elapsed = int((time.monotonic() - start) * 1000)

        status.reachable = True
        status.latency_ms = elapsed
    except Exception as exc:
        status.reachable = False
        status.last_error = str(exc)

    return status


def _check_smtp(test_connectivity: bool) -> ChannelStatus:
    """Check SMTP email configuration."""
    status = ChannelStatus(channel="email_smtp")

    smtp_host = _env_first(["MAIL_HOST", "MAIL_SERVER", "SMTP_HOST"]).strip()
    smtp_port_str = _env_first(["MAIL_PORT", "SMTP_PORT"]).strip()
    smtp_user = _env_first(["MAIL_USER", "MAIL_USERNAME", "SMTP_USER"]).strip()

    if not smtp_host:
        status.configured = False
        status.details["env_vars"] = ["MAIL_HOST", "MAIL_SERVER", "SMTP_HOST"]
        status.details["hint"] = "Set MAIL_HOST and related SMTP environment variables"
        return status

    status.configured = True
    status.details["host"] = smtp_host
    status.details["port"] = int(smtp_port_str or "587")
    status.details["user"] = smtp_user[:3] + "***" if smtp_user else "(not set)"

    if not test_connectivity:
        return status

    smtp_port = int(smtp_port_str or "587")
    try:
        start = time.monotonic()
        sock = socket.create_connection((smtp_host, smtp_port), timeout=5)
        sock.close()
        elapsed = int((time.monotonic() - start) * 1000)

        status.reachable = True
        status.latency_ms = elapsed
    except Exception as exc:
        status.reachable = False
        status.last_error = str(exc)

    return status


def _check_wecom_user(user_id: str, test_connectivity: bool) -> ChannelStatus:
    """Check user-specific WeCom webhook from database."""
    status = ChannelStatus(channel="wecom_user")

    try:
        from api.services.auth_service import decrypt_secret
        from api.database import UserLLMConfigDB, get_db_ctx

        with get_db_ctx() as db:
            user_cfg = db.query(UserLLMConfigDB).filter(
                UserLLMConfigDB.user_id == user_id
            ).first()

            if not user_cfg or not user_cfg.wecom_webhook_encrypted:
                status.configured = False
                status.details["hint"] = "Configure WeCom webhook in user settings"
                return status

            webhook_url = decrypt_secret(user_cfg.wecom_webhook_encrypted)
            if not webhook_url:
                status.configured = False
                status.details["hint"] = "WeCom webhook decryption failed"
                status.last_error = "decryption_failed"
                return status

            status.configured = True
            status.details["url_masked"] = _mask_url(webhook_url)

            if test_connectivity:
                from urllib.parse import urlparse
                parsed = urlparse(webhook_url)
                host = parsed.hostname or ""
                port = parsed.port or 443

                start = time.monotonic()
                sock = socket.create_connection((host, port), timeout=5)
                sock.close()
                elapsed = int((time.monotonic() - start) * 1000)

                status.reachable = True
                status.latency_ms = elapsed

    except Exception as exc:
        status.configured = False
        status.last_error = str(exc)

    return status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_first(keys: list[str], default: str = "") -> str:
    """Return the first non-empty env var from the list."""
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def _mask_url(url: str) -> str:
    """Mask a URL to hide sensitive parts (e.g. webhook key)."""
    if not url:
        return ""
    # Show scheme + host, mask the rest
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "***"
        return f"{parsed.scheme}://{host}/**masked**"
    except Exception:
        return url[:20] + "***"
