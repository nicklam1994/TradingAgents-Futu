# -*- coding: utf-8 -*-
"""
飞书发送器

通过飞书自定义机器人 Webhook 发送交互卡片消息。
支持签名校验和关键词前缀。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any, Dict, Optional

import requests

from tradingagents.notification.core import NotificationChannel, Sender

logger = logging.getLogger(__name__)

# 飞书单条消息字节上限（保守值）
_FEISHU_MAX_BYTES = 20000


def _format_feishu_markdown(content: str) -> str:
    """将标准 Markdown 转为飞书 lark_md 兼容格式。"""
    import re
    # 飞书 lark_md 不支持 **bold**，用 **text** 保持（飞书部分支持）
    # 移除不支持的语法
    result = content
    # 代码块转为普通文本
    result = re.sub(r"```[\s\S]*?```", lambda m: m.group(0).replace("```", ""), result)
    return result


class FeishuSender(Sender):
    """飞书自定义机器人 Webhook 发送器。"""

    channel = NotificationChannel.FEISHU

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._webhook_url: str = config.get("feishu_webhook_url", "") or ""
        self._secret: str = config.get("feishu_webhook_secret", "") or ""
        self._keyword: str = config.get("feishu_webhook_keyword", "") or ""
        self._max_bytes: int = int(config.get("feishu_max_bytes", _FEISHU_MAX_BYTES))
        self._verify_ssl: bool = config.get("webhook_verify_ssl", True)

    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    def _build_security_fields(self) -> Dict[str, str]:
        """构建飞书签名字段。"""
        if not self._secret:
            return {}
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{self._secret}"
        sign = base64.b64encode(
            hmac.new(
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return {"timestamp": timestamp, "sign": sign}

    def _apply_keyword_prefix(self, content: str) -> str:
        """添加关键词前缀以通过飞书安全检查。"""
        if not self._keyword:
            return content
        return f"{self._keyword}\n{content}" if content else self._keyword

    def send(
        self,
        content: str,
        *,
        title: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        image_bytes: Optional[bytes] = None,
    ) -> bool:
        if not self.is_configured():
            logger.warning("飞书 Webhook 未配置，跳过推送")
            return False

        formatted = _format_feishu_markdown(content)
        formatted = self._apply_keyword_prefix(formatted)

        # 分段发送
        chunks = self._split_content(formatted, self._max_bytes)
        all_ok = True
        for chunk in chunks:
            payload: Dict[str, Any] = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": title or "TradingAgents 通知",
                        }
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {"tag": "lark_md", "content": chunk},
                        }
                    ],
                },
            }
            # 添加签名
            security = self._build_security_fields()
            if security:
                payload.update(security)

            try:
                resp = requests.post(
                    self._webhook_url,
                    json=payload,
                    timeout=timeout_seconds or 10,
                    verify=self._verify_ssl,
                )
                data = resp.json()
                if data.get("code", 0) != 0:
                    logger.error("飞书发送失败: code=%s", data.get("code"))
                    all_ok = False
                else:
                    logger.info("飞书消息发送成功")
            except Exception as exc:
                logger.error("飞书发送异常: %s", type(exc).__name__)
                all_ok = False

        return all_ok

    @staticmethod
    def _split_content(content: str, max_bytes: int) -> list[str]:
        """按字节长度分段。"""
        lines = content.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_bytes = 0

        for line in lines:
            line_bytes = len(line.encode("utf-8")) + 1
            if current_bytes + line_bytes > max_bytes and current:
                chunks.append("\n".join(current))
                current = [line]
                current_bytes = line_bytes
            else:
                current.append(line)
                current_bytes += line_bytes

        if current:
            chunks.append("\n".join(current))
        return chunks or [content]
