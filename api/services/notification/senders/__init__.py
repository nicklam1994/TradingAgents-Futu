"""Notification senders package.

Each sender implements the ``NotificationSender`` ABC for its channel.
Import individual senders from their submodules, or use ``register_all``
to wire them into the global ``notification_service``.
"""

from api.services.notification.senders.email_sender import EmailSender
from api.services.notification.senders.wechat_sender import WechatSender
from api.services.notification.senders.feishu_sender import FeishuSender
from api.services.notification.senders.telegram_sender import TelegramSender
from api.services.notification.senders.discord_sender import DiscordSender
from api.services.notification.senders.slack_sender import SlackSender
from api.services.notification.senders.custom_webhook_sender import CustomWebhookSender

__all__ = [
    "EmailSender",
    "WechatSender",
    "FeishuSender",
    "TelegramSender",
    "DiscordSender",
    "SlackSender",
    "CustomWebhookSender",
]


def register_all(service=None) -> None:
    """Register all senders with the global notification service."""
    from api.services.notification.notification_channel import (
        NotificationChannel,
        notification_service as _svc,
    )

    svc = service or _svc
    svc.register(NotificationChannel.EMAIL, EmailSender())
    svc.register(NotificationChannel.WECHAT, WechatSender())
    svc.register(NotificationChannel.FEISHU, FeishuSender())
    svc.register(NotificationChannel.TELEGRAM, TelegramSender())
    svc.register(NotificationChannel.DISCORD, DiscordSender())
    svc.register(NotificationChannel.SLACK, SlackSender())
    svc.register(NotificationChannel.CUSTOM_WEBHOOK, CustomWebhookSender())
