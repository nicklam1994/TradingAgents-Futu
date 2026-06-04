"""Search service framework — provider ABC, data classes, and orchestrator.

This module defines the abstraction layer for web search providers used by
TradingAgents to fetch stock news and market intelligence.  Each concrete
provider (Tavily, Brave, SerpAPI, SearXNG, Bocha, Anspire, MiniMax) implements
the ``SearchProvider`` ABC.  The ``SearchService`` class orchestrates them with
priority-based rotation, multi-key load balancing, TTL caching, and retry logic.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ─── Data classes ────────────────────────────────────────────────────────────


@dataclass
class SearchResult:
    """A single search result from any provider."""

    title: str
    snippet: str
    url: str
    source: str = ""
    published_date: str = ""


@dataclass
class SearchResponse:
    """Envelope returned by every SearchProvider.search() call."""

    query: str
    results: List[SearchResult] = field(default_factory=list)
    provider: str = ""
    success: bool = True
    error_message: str = ""
    search_time: float = 0.0


# ─── Provider ABC ────────────────────────────────────────────────────────────


class SearchProvider(ABC):
    """Abstract base for search providers.

    Every concrete provider must implement ``search()`` and expose a ``name``
    property.  The ``_keys`` helper loads comma-separated API keys from the
    given environment variable name.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier (e.g. 'tavily', 'brave')."""
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        """Execute a search query and return a ``SearchResponse``.

        Implementations should catch all exceptions and return a failed
        ``SearchResponse`` rather than propagating errors, so the orchestrator
        can fall through to the next provider.
        """
        raise NotImplementedError

    @staticmethod
    def _keys(env_var: str) -> List[str]:
        """Load comma-separated API keys from *env_var*.

        Returns an empty list when the variable is unset or empty.
        """
        raw = os.getenv(env_var, "")
        return [k.strip() for k in raw.split(",") if k.strip()]
