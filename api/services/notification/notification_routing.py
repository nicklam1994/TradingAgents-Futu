"""Notification routing configuration.

Defines 3 route types and determines which channels each route type
dispatches to, based on user configuration stored in the DB.

Route types
-----------
- ``REPORT``:  periodic analysis reports (scheduled daily runs, manual runs)
- ``ALERT``:   price alerts, watchlist triggers, risk threshold breaches
- ``SYSTEM_ERROR``: system-level errors (failed analyses, service outages)

Routing config lives in the ``user_llm_configs.notification_config`` JSON column.
When absent, sensible defaults apply (email for REPORT, wechat for ALERT).
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from api.services.notification.notification_channel import NotificationChannel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Route types
# ---------------------------------------------------------------------------

class RouteType(str, Enum):
    """Notification route types."""

    REPORT = "REPORT"
    ALERT = "ALERT"
    SYSTEM_ERROR = "SYSTEM_ERROR"


# ---------------------------------------------------------------------------
# Default routing table
# ---------------------------------------------------------------------------

# Each route type maps to a list of (channel, enabled_by_default) tuples.
# Users can override per-channel enabled/disabled in their notification_config.
_DEFAULT_ROUTING: Dict[str, List[NotificationChannel]] = {
    RouteType.REPORT.value: [
        NotificationChannel.EMAIL,
        NotificationChannel.WECHAT,
    ],
    RouteType.ALERT.value: [
        NotificationChannel.WECHAT,
        NotificationChannel.TELEGRAM,
    ],
    RouteType.SYSTEM_ERROR.value: [
        NotificationChannel.WECHAT,
    ],
}


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------

def get_channels_for_route(
    route_type: str,
    user_config: Dict[str, Any],
) -> List[NotificationChannel]:
    """Return the list of channels that should receive a message for *route_type*.

    *user_config* is the parsed ``notification_config`` JSON from the user's
    ``user_llm_configs`` row.  Shape::

        {
            "routes": {
                "REPORT": ["email", "wechat", "telegram"],
                "ALERT": ["wechat", "telegram"],
                "SYSTEM_ERROR": ["wechat"],
            },
            "email": {"enabled": true, ...},
            "wechat": {"enabled": true, ...},
            ...
        }

    If ``routes`` is missing or doesn't contain the requested *route_type*,
    the defaults from ``_DEFAULT_ROUTING`` are used.  Channels listed in
    ``routes`` but missing from *user_config* or with ``enabled: false`` are
    silently skipped.
    """
    routes_cfg = user_config.get("routes", {})
    channel_names: List[str] = routes_cfg.get(
        route_type,
        [ch.value for ch in _DEFAULT_ROUTING.get(route_type, [])],
    )

    result: List[NotificationChannel] = []
    for name in channel_names:
        try:
            channel = NotificationChannel(name)
        except ValueError:
            logger.warning("[routing] unknown channel %r in route %s", name, route_type)
            continue

        # Only include if the channel is enabled in user config
        ch_config = user_config.get(name, {})
        if ch_config.get("enabled", False):
            result.append(channel)

    return result


def get_default_routing() -> Dict[str, List[str]]:
    """Return the default routing table as plain dicts (for API responses)."""
    return {
        route: [ch.value for ch in channels]
        for route, channels in _DEFAULT_ROUTING.items()
    }


# ---------------------------------------------------------------------------
# DB helpers — read / write notification_config
# ---------------------------------------------------------------------------

def load_notification_config(db: "Session", user_id: str) -> Dict[str, Any]:
    """Load the notification config for *user_id* from the DB.

    Returns an empty dict if no config is stored yet.
    """
    from api.database import UserLLMConfigDB

    row = db.query(UserLLMConfigDB).filter(
        UserLLMConfigDB.user_id == user_id
    ).first()
    if row is None:
        return {}

    raw = getattr(row, "notification_config", None)
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        logger.warning("[routing] invalid notification_config JSON for user %s", user_id)
        return {}


def save_notification_config(
    db: "Session",
    user_id: str,
    config: Dict[str, Any],
) -> None:
    """Persist *config* as the notification config for *user_id*."""
    from api.database import UserLLMConfigDB

    row = db.query(UserLLMConfigDB).filter(
        UserLLMConfigDB.user_id == user_id
    ).first()
    if row is None:
        logger.error("[routing] user %s not found, cannot save notification config", user_id)
        return

    row.notification_config = json.dumps(config, ensure_ascii=False)
    db.commit()
