# -*- coding: utf-8 -*-
"""
通知发送器注册表

提供所有内置 Sender 的导入入口。
"""

from tradingagents.notification.senders.email_sender import EmailSender
from tradingagents.notification.senders.wechat_sender import WechatSender
from tradingagents.notification.senders.feishu_sender import FeishuSender
from tradingagents.notification.senders.telegram_sender import TelegramSender
from tradingagents.notification.senders.discord_sender import DiscordSender
from tradingagents.notification.senders.slack_sender import SlackSender

__all__ = [
    "EmailSender",
    "WechatSender",
    "FeishuSender",
    "TelegramSender",
    "DiscordSender",
    "SlackSender",
]
