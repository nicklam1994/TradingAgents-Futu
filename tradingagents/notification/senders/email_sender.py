# -*- coding: utf-8 -*-
"""
Email 发送器

通过 SMTP 发送邮件通知。
支持 QQ、163、Gmail、Outlook 等主流邮箱自动识别 SMTP 配置。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Dict, List, Optional

from tradingagents.notification.core import NotificationChannel, Sender

logger = logging.getLogger(__name__)

# SMTP 服务器自动识别配置
SMTP_CONFIGS: Dict[str, Dict[str, Any]] = {
    "qq.com": {"server": "smtp.qq.com", "port": 465, "ssl": True},
    "foxmail.com": {"server": "smtp.qq.com", "port": 465, "ssl": True},
    "163.com": {"server": "smtp.163.com", "port": 465, "ssl": True},
    "126.com": {"server": "smtp.126.com", "port": 465, "ssl": True},
    "gmail.com": {"server": "smtp.gmail.com", "port": 587, "ssl": False},
    "outlook.com": {"server": "smtp-mail.outlook.com", "port": 587, "ssl": False},
    "hotmail.com": {"server": "smtp-mail.outlook.com", "port": 587, "ssl": False},
    "live.com": {"server": "smtp-mail.outlook.com", "port": 587, "ssl": False},
    "sina.com": {"server": "smtp.sina.com", "port": 465, "ssl": True},
    "sohu.com": {"server": "smtp.sohu.com", "port": 465, "ssl": True},
    "aliyun.com": {"server": "smtp.aliyun.com", "port": 465, "ssl": True},
    "139.com": {"server": "smtp.139.com", "port": 465, "ssl": True},
}


def _detect_smtp_config(sender_email: str) -> Dict[str, Any]:
    """根据邮箱域名自动识别 SMTP 配置。"""
    domain = sender_email.split("@")[-1].lower() if "@" in sender_email else ""
    return SMTP_CONFIGS.get(domain, {
        "server": f"smtp.{domain}",
        "port": 465,
        "ssl": True,
    })


class EmailSender(Sender):
    """SMTP 邮件发送器。"""

    channel = NotificationChannel.EMAIL

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._sender: str = config.get("email_sender", "") or ""
        self._sender_name: str = config.get("email_sender_name", "TradingAgents 助手") or ""
        self._password: str = config.get("email_password", "") or ""
        self._receivers: List[str] = config.get("email_receivers", []) or []
        # 若未指定收件人，默认发给自己
        if not self._receivers and self._sender:
            self._receivers = [self._sender]

    def is_configured(self) -> bool:
        return bool(self._sender and self._password)

    def send(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        image_bytes: Optional[bytes] = None,
    ) -> bool:
        if not self.is_configured():
            logger.warning("邮件配置不完整，跳过推送")
            return False

        subject = title or "TradingAgents 通知"
        smtp_cfg = _detect_smtp_config(self._sender)
        server: Optional[smtplib.SMTP] = None

        try:
            # 构建邮件
            msg = MIMEMultipart("alternative")
            msg["Subject"] = str(Header(subject, "utf-8"))
            msg["From"] = formataddr((
                str(Header(self._sender_name, "utf-8")),
                self._sender,
            ))
            msg["To"] = ", ".join(self._receivers)

            # 纯文本 + HTML
            msg.attach(MIMEText(content, "plain", "utf-8"))
            html_content = self._markdown_to_simple_html(content)
            msg.attach(MIMEText(html_content, "html", "utf-8"))

            # 连接 SMTP
            timeout = int(timeout_seconds or 30)
            if smtp_cfg.get("ssl"):
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(
                    smtp_cfg["server"], smtp_cfg["port"], timeout=timeout, context=context,
                )
            else:
                server = smtplib.SMTP(smtp_cfg["server"], smtp_cfg["port"], timeout=timeout)
                server.ehlo()
                server.starttls()

            server.login(self._sender, self._password)
            server.sendmail(self._sender, self._receivers, msg.as_string())
            logger.info("邮件发送成功 -> %s", ", ".join(self._receivers))
            return True

        except Exception as exc:
            logger.error("邮件发送失败: %s", exc)
            return False
        finally:
            self._close_server(server)

    @staticmethod
    def _close_server(server: Optional[smtplib.SMTP]) -> None:
        if server is None:
            return
        try:
            server.quit()
        except Exception:
            try:
                server.close()
            except Exception:
                pass

    @staticmethod
    def _markdown_to_simple_html(md: str) -> str:
        """基础 Markdown -> HTML 转换。"""
        import re
        lines: List[str] = []
        for line in md.split("\n"):
            m = re.match(r"^(#{1,6})\s+(.+)$", line)
            if m:
                level = len(m.group(1))
                lines.append(f"<h{level}>{m.group(2)}</h{level}>")
                continue
            if line.strip() in ("---", "***", "___"):
                lines.append("<hr>")
                continue
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            line = re.sub(r"\*(.+?)\*", r"<em>\1</em>", line)
            line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
            if line.strip():
                lines.append(f"<p>{line}</p>")
            else:
                lines.append("")
        return "<html><body>" + "\n".join(lines) + "</body></html>"
