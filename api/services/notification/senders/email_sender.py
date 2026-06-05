"""Email notification sender via SMTP.

Integrates with the existing ``email_report_service`` for HTML rendering.
This sender adapts the ``NotificationSender`` ABC to SMTP delivery.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any, Dict

from api.services.notification.notification_channel import (
    NotificationChannel,
    NotificationSender,
)

logger = logging.getLogger(__name__)


def _get_env(keys: list[str], default: str = "") -> str:
    """Return the first non-None env var from *keys*, else *default*."""
    for k in keys:
        v = os.getenv(k)
        if v is not None:
            return v
    return default


class EmailSender(NotificationSender):
    """Send report emails via SMTP.

    Config keys:
        - ``enabled`` (bool): whether this channel is active
        - ``address`` (str): recipient email address
        - ``subject_prefix`` (str, optional): prepend to subject line
    """

    channel = NotificationChannel.EMAIL

    def send(self, message: str, config: Dict[str, Any]) -> bool:
        """Send *message* (HTML body) via SMTP."""
        address = config.get("address", "")
        if not address:
            logger.warning("[email_sender] no recipient address, skipping")
            return False

        smtp_host = _get_env(["MAIL_HOST", "MAIL_SERVER", "SMTP_HOST"]).strip()
        if not smtp_host:
            logger.info("[email_sender] SMTP not configured, skipping")
            return False

        smtp_port = int(_get_env(["MAIL_PORT", "SMTP_PORT"]) or "587")
        smtp_user = _get_env(["MAIL_USER", "MAIL_USERNAME", "SMTP_USER"]).strip()
        smtp_password = _get_env(["MAIL_PASS", "MAIL_PASSWORD", "SMTP_PASSWORD"]).strip()
        smtp_from = _get_env(["MAIL_FROM", "SMTP_FROM"], smtp_user or "noreply@example.com").strip()

        starttls = _get_env(["MAIL_STARTTLS", "SMTP_TLS"], "1").strip().lower() not in ("0", "false", "off")
        ssl_tls = _get_env(["MAIL_SSL", "MAIL_SSL_TLS"], "0").strip().lower() in ("1", "true", "on")

        prefix = config.get("subject_prefix", "TradingAgents 投研报告")

        msg = EmailMessage()
        msg["Subject"] = prefix
        msg["From"] = smtp_from
        msg["To"] = address
        msg.set_content("请使用支持 HTML 的邮件客户端查看此报告。")
        msg.add_alternative(message, subtype="html")

        try:
            logger.info("[email_sender] sending to %s via %s:%s", address, smtp_host, smtp_port)
            smtp_cls = smtplib.SMTP_SSL if ssl_tls else smtplib.SMTP
            with smtp_cls(smtp_host, smtp_port, timeout=20) as server:
                if starttls and not ssl_tls:
                    server.starttls()
                if smtp_user:
                    server.login(smtp_user, smtp_password)
                server.send_message(msg)
            logger.info("[email_sender] sent OK to %s", address)
            return True
        except Exception as exc:
            logger.error("[email_sender] failed to send to %s: %s", address, exc)
            return False
