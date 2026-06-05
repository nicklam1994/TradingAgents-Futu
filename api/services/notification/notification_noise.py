"""Notification noise / deduplication filter.

Prevents the same notification from being sent multiple times within a
configurable time window.  The filter is keyed on
``(symbol, route_type, channel)`` and uses an in-memory dict with TTL
expiry — no DB table required.

If the process restarts, the dedup state resets, which is acceptable:
a single duplicate after restart is tolerable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default dedup window per route type (seconds).
# REPORT: 60 min — don't re-send the same report for the same symbol.
# ALERT:  15 min — rate-limit alert spam.
# SYSTEM_ERROR: 30 min — avoid flooding on cascading failures.
_DEFAULT_WINDOWS: Dict[str, int] = {
    "REPORT": 3600,
    "ALERT": 900,
    "SYSTEM_ERROR": 1800,
}


# ---------------------------------------------------------------------------
# Dedup store
# ---------------------------------------------------------------------------

@dataclass
class _DedupEntry:
    """A single dedup record."""
    last_sent: float
    window: int

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.last_sent) > self.window


@dataclass
class _NoiseStore:
    """Thread-safe in-memory dedup store."""

    _entries: Dict[Tuple[str, str, str], _DedupEntry] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def should_send(self, symbol: str, route_type: str, channel: str) -> bool:
        """Return True if the message should be sent (no recent duplicate)."""
        key = (symbol.upper(), route_type, channel)
        window = _DEFAULT_WINDOWS.get(route_type, 3600)

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return True
            if entry.expired:
                # Expired — allow sending, remove stale entry
                del self._entries[key]
                return True
            remaining = window - (time.monotonic() - entry.last_sent)
            logger.info(
                "[noise] suppressed %s for %s/%s — %.0fs remaining in window",
                channel, symbol, route_type, remaining,
            )
            return False

    def record_sent(self, symbol: str, route_type: str, channel: str) -> None:
        """Record that a message was just sent for this key."""
        key = (symbol.upper(), route_type, channel)
        window = _DEFAULT_WINDOWS.get(route_type, 3600)

        with self._lock:
            self._entries[key] = _DedupEntry(
                last_sent=time.monotonic(),
                window=window,
            )

    def cleanup(self) -> int:
        """Remove expired entries. Returns the count of removed entries."""
        removed = 0
        with self._lock:
            expired_keys = [k for k, v in self._entries.items() if v.expired]
            for k in expired_keys:
                del self._entries[k]
                removed += 1
        return removed

    @property
    def size(self) -> int:
        """Number of active dedup entries."""
        with self._lock:
            return len(self._entries)


# Module-level singleton
_noise_store = _NoiseStore()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def should_send(symbol: str, route_type: str, channel: str) -> bool:
    """Check whether a notification should be sent or suppressed."""
    return _noise_store.should_send(symbol, route_type, channel)


def record_sent(symbol: str, route_type: str, channel: str) -> None:
    """Record that a notification was successfully sent."""
    _noise_store.record_sent(symbol, route_type, channel)


def cleanup_expired() -> int:
    """Manually trigger cleanup of expired dedup entries."""
    return _noise_store.cleanup()


def get_dedup_stats() -> Dict[str, int]:
    """Return current dedup store statistics (for monitoring / debugging)."""
    return {"active_entries": _noise_store.size}
