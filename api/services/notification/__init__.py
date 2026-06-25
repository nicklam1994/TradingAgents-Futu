"""Notification subsystem — alert rules, message builder, and workers.

The core notification framework (channels, senders, routing, noise control)
lives in ``tradingagents.notification``. The API bridge is in
``api.services.notification_bridge``.

This package provides:
- ``NotificationBuilder`` — renders ReportDB into plain/markdown/HTML text
- ``AlertService`` / ``AlertWorker`` — alert rule evaluation and dispatch
- ``markdown_to_image`` — Markdown-to-PNG rendering
"""

from api.services.notification.notification_builder import NotificationBuilder

__all__ = [
    "NotificationBuilder",
]
