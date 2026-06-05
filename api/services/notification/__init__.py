"""Notification subsystem — channels, routing, senders, and deduplication.

Usage::

    from api.services.notification import notification_service
    from api.services.notification.notification_channel import NotificationChannel
    from api.services.notification.notification_routing import RouteType, load_notification_config

    # Load user config from DB
    config = load_notification_config(db, user_id)

    # Dispatch a report
    results = await notification_service.dispatch(
        report, route_type=RouteType.REPORT.value, user_config=config,
    )
"""

from api.services.notification.notification_channel import (
    NotificationChannel,
    NotificationSender,
    NotificationService,
    NotificationBuilder,
    notification_service,
)

from api.services.notification.notification_routing import (
    RouteType,
    get_channels_for_route,
    get_default_routing,
    load_notification_config,
    save_notification_config,
)

from api.services.notification.notification_noise import (
    should_send,
    record_sent,
    cleanup_expired,
    get_dedup_stats,
)

__all__ = [
    # Channel / Sender / Service
    "NotificationChannel",
    "NotificationSender",
    "NotificationService",
    "NotificationBuilder",
    "notification_service",
    # Routing
    "RouteType",
    "get_channels_for_route",
    "get_default_routing",
    "load_notification_config",
    "save_notification_config",
    # Noise
    "should_send",
    "record_sent",
    "cleanup_expired",
    "get_dedup_stats",
]
