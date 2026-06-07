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
from typing import Any, Dict, List, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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


@dataclass
class _Result:
    """Holder for in-flight dedup results.

    Uses a dedicated wrapper so the result reference stays valid even after
    the inflight dict entry is deleted — waiters hold a local reference to
    this object and read the result from it rather than re-looking up the dict.
    """

    response: Optional[SearchResponse] = None


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


# ─── Orchestrator ────────────────────────────────────────────────────────────


class _TransientSearchError(Exception):
    """Raised when a search provider fails transiently (network, rate-limit).

    Tenacity retries only on this type — permanent failures (no keys, invalid
    config) return a failed ``SearchResponse`` directly without retrying.
    """


# Priority order: Anspire → Bocha → Tavily → Brave → SerpAPI → MiniMax → SearXNG
_PROVIDER_PRIORITY = [
    "anspire",
    "bocha",
    "tavily",
    "brave",
    "serpapi",
    "minimax",
    "searxng",
]

# Env var names for each provider's API keys / base URLs
_PROVIDER_ENV = {
    "anspire": "ANSPIRE_API_KEYS",
    "bocha": "BOCHA_API_KEYS",
    "tavily": "TAVILY_API_KEYS",
    "brave": "BRAVE_API_KEYS",
    "serpapi": "SERPAPI_API_KEYS",
    "minimax": "MINIMAX_API_KEYS",
    "searxng": "SEARXNG_BASE_URLS",
}


class SearchService:
    """Orchestrates multiple search providers with fallback, caching, and retry.

    Features:
      - **Priority rotation**: providers tried in fixed order; first success wins.
      - **Multi-key load balancing**: each provider's keys rotate round-robin.
      - **TTL cache**: identical queries within ``cache_ttl`` seconds return
        cached results (thread-safe via ``threading.Lock``).
      - **Concurrent dedup**: if the same query is already in-flight, wait for
        its result instead of sending a duplicate request.
      - **tenacity retry**: transient errors (network, rate-limit) trigger
        exponential backoff; permanent failures (no keys) skip immediately.
      - **Fallback**: when a provider fails all retries, the next provider
        in the priority list is tried.
    """

    def __init__(self, cache_ttl: int = 1800, search_config: Optional[Dict[str, Any]] = None) -> None:
        """Initialise the search service.

        Args:
            cache_ttl: Cache time-to-live in seconds (default 1800 = 30 min).
            search_config: Optional DB config dict from user_llm_configs.search_config.
                          When provided, injects API keys into environment variables
                          so providers can read them via os.getenv().
        """
        # Inject DB search config into env vars so providers can read them
        if search_config:
            self._inject_config_to_env(search_config)

        self._cache_ttl = cache_ttl
        self._cache: Dict[str, tuple[float, SearchResponse]] = {}
        self._cache_lock = threading.Lock()

        # Per-provider key rotation index (provider_name → current index)
        self._key_indices: Dict[str, int] = {}
        self._key_lock = threading.Lock()

        # In-flight dedup: query_hash → (threading.Event, _Result holder)
        self._inflight: Dict[str, tuple[threading.Event, _Result]] = {}
        self._inflight_lock = threading.Lock()

        # Lazy-loaded provider instances (name → instance)
        self._providers: Dict[str, SearchProvider] = {}

    # ── Config injection ────────────────────────────────────────────────────

    @staticmethod
    def _inject_config_to_env(search_config: Dict[str, Any]) -> None:
        """Inject DB search config into environment variables.

        Maps provider name → env var name and sets os.environ if not already set.
        """
        _CONFIG_TO_ENV = {
            "tavily": "TAVILY_API_KEYS",
            "brave": "BRAVE_API_KEYS",
            "serpapi": "SERPAPI_API_KEYS",
            "bocha": "BOCHA_API_KEYS",
            "anspire": "ANSPIRE_API_KEYS",
            "minimax": "MINIMAX_API_KEYS",
            "searxng": "SEARXNG_BASE_URLS",
        }
        for name, env_var in _CONFIG_TO_ENV.items():
            provider_cfg = search_config.get(name, {})
            api_key = (provider_cfg.get("api_key") or "").strip()
            enabled = provider_cfg.get("enabled", True)
            if api_key and enabled and not os.environ.get(env_var):
                os.environ[env_var] = api_key
                logger.info("[SearchService] Injected %s from DB config", env_var)

    # ── Provider instantiation (lazy) ────────────────────────────────────────

    def _get_provider(self, name: str) -> Optional[SearchProvider]:
        """Return a provider instance, creating it lazily on first access."""
        if name not in self._providers:
            prov = self._create_provider(name)
            if prov is None:
                return None
            self._providers[name] = prov
        return self._providers[name]

    @staticmethod
    def _create_provider(name: str) -> Optional[SearchProvider]:
        """Factory: import and instantiate a provider by name."""
        # Lazy imports to avoid pulling all SDKs at module load time
        from .search_providers.anspire_provider import AnspireSearchProvider
        from .search_providers.bocha_provider import BochaSearchProvider
        from .search_providers.brave_provider import BraveSearchProvider
        from .search_providers.minimax_provider import MiniMaxSearchProvider
        from .search_providers.searxng_provider import SearXNGSearchProvider
        from .search_providers.serpapi_provider import SerpAPISearchProvider
        from .search_providers.tavily_provider import TavilySearchProvider

        factories = {
            "anspire": AnspireSearchProvider,
            "bocha": BochaSearchProvider,
            "tavily": TavilySearchProvider,
            "brave": BraveSearchProvider,
            "serpapi": SerpAPISearchProvider,
            "minimax": MiniMaxSearchProvider,
            "searxng": SearXNGSearchProvider,
        }
        factory = factories.get(name)
        if factory is None:
            logger.warning("Unknown search provider: %s", name)
            return None
        return factory()

    # ── Key rotation ─────────────────────────────────────────────────────────

    def _next_key(self, provider_name: str) -> Optional[str]:
        """Return the next API key for *provider_name* (round-robin).

        Returns ``None`` when no keys are configured for this provider.
        """
        env_var = _PROVIDER_ENV.get(provider_name, "")
        if not env_var:
            return None

        raw = os.getenv(env_var, "")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not keys:
            return None

        with self._key_lock:
            idx = self._key_indices.get(provider_name, 0) % len(keys)
            self._key_indices[provider_name] = idx + 1
            return keys[idx]

    def _has_keys(self, provider_name: str) -> bool:
        """Check if *provider_name* has any API keys configured."""
        env_var = _PROVIDER_ENV.get(provider_name, "")
        if not env_var:
            return False
        return bool(os.getenv(env_var, "").strip())

    # ── Cache ────────────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(query: str, max_results: int) -> str:
        """Build a deterministic cache key from query parameters."""
        return hashlib.sha256(f"{query}:{max_results}".encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[SearchResponse]:
        """Return cached response if still valid, else ``None``."""
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            ts, resp = entry
            if time.monotonic() - ts > self._cache_ttl:
                del self._cache[key]
                return None
            return resp

    def _cache_put(self, key: str, resp: SearchResponse) -> None:
        """Store a successful response in the cache."""
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), resp)

    # ── In-flight dedup ─────────────────────────────────────────────────────

    def _dedup_or_acquire(self, key: str) -> Optional[SearchResponse]:
        """Check if *key* is already in-flight.

        Returns:
            ``None`` if this caller should execute the search (it now owns the
            in-flight slot).  Otherwise returns the result from the first
            caller that completed the search.

        Race-condition fix: waiters hold a local reference to the ``_Result``
        holder.  ``_dedup_complete`` writes the result into the holder *before*
        setting the event and *before* deleting the inflight entry.  After
        ``event.wait()`` returns, the waiter reads from its local holder
        reference — no dict re-lookup needed.
        """
        with self._inflight_lock:
            if key in self._inflight:
                # Another thread is already searching — grab references
                event, result = self._inflight[key]
            else:
                # We are the first — create the slot
                event = threading.Event()
                result = _Result()
                self._inflight[key] = (event, result)
                return None  # Caller should execute

        # Wait outside the lock (another thread owns the slot).
        # Read from our local ``result`` reference — it stays valid even if
        # ``_dedup_complete`` deletes the inflight entry.
        event.wait(timeout=30)
        return result.response

    def _dedup_complete(self, key: str, resp: SearchResponse) -> None:
        """Publish the result for an in-flight query and wake waiters.

        Order matters to avoid the race condition:
        1. Write result into the holder (waiters have a live reference).
        2. Set the event (waiters wake up and read from their holder).
        3. Delete the inflight entry (cleanup).
        """
        with self._inflight_lock:
            entry = self._inflight.get(key)
            if entry is None:
                return
            event, result = entry
            # Step 1: store result BEFORE waking waiters
            result.response = resp
            # Step 2: wake waiters — they read from their local _Result ref
            event.set()
            # Step 3: cleanup (waiters don't need the dict entry anymore)
            del self._inflight[key]

    # ── Core search with retry ───────────────────────────────────────────────

    def _search_with_retry(
        self, provider: SearchProvider, query: str, max_results: int
    ) -> SearchResponse:
        """Search with *provider*, retrying transient errors via tenacity.

        The tenacity decorator is applied inline so each call gets a fresh
        retry state.  Only ``_TransientSearchError`` triggers retries.
        """

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(_TransientSearchError),
            reraise=True,
        )
        def _do_search() -> SearchResponse:
            # Rotate API key before each attempt
            self._set_provider_key(provider)
            resp = provider.search(query, max_results)

            if resp.success:
                return resp

            # Classify the error
            err = resp.error_message.lower()
            transient_signals = [
                "timeout", "rate limit", "429", "503", "502",
                "connection", "network", "temporary",
            ]
            if any(sig in err for sig in transient_signals):
                raise _TransientSearchError(resp.error_message)

            # Permanent failure — no retry
            return resp

        try:
            return _do_search()
        except _TransientSearchError:
            return SearchResponse(
                query=query,
                provider=provider.name,
                success=False,
                error_message="All retries exhausted",
            )

    def _set_provider_key(self, provider: SearchProvider) -> None:
        """Inject the next API key into the provider's environment.

        Providers read keys from env vars at call time.  For round-robin we
        temporarily set the env var to the next key before each search call.
        This is safe because each thread holds its own key reference.
        """
        env_var = _PROVIDER_ENV.get(provider.name, "")
        if not env_var:
            return
        key = self._next_key(provider.name)
        if key:
            os.environ[env_var] = key

    # ── Public API ───────────────────────────────────────────────────────────

    def search(
        self, query: str, max_results: int = 5
    ) -> SearchResponse:
        """Search across all providers — multi-source aggregation + SearXNG fallback.

        1. Check TTL cache → return if hit.
        2. Check in-flight dedup → wait if same query is running.
        3. Call ALL configured providers (except SearXNG) in parallel.
        4. Merge results from all successful providers (dedup by URL).
        5. If no results from paid providers → fall back to SearXNG (mandatory).
        6. Cache and return merged response.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        ckey = self._cache_key(query, max_results)

        # 1. Cache check
        cached = self._cache_get(ckey)
        if cached is not None:
            logger.debug("Cache hit for query: %s", query[:60])
            return cached

        # 2. In-flight dedup
        dedup_result = self._dedup_or_acquire(ckey)
        if dedup_result is not None:
            return dedup_result

        # 3. Collect paid providers (all except searxng)
        paid_providers = []
        for pname in _PROVIDER_PRIORITY:
            if pname == "searxng":
                continue
            if not self._has_keys(pname):
                continue
            provider = self._get_provider(pname)
            if provider is not None:
                paid_providers.append((pname, provider))

        # 4. Call all paid providers in parallel
        all_results: List[SearchResult] = []
        seen_urls: set = set()
        providers_used: List[str] = []
        errors: List[str] = []

        def _try_provider(pname_provider):
            pname, provider = pname_provider
            logger.info("Searching: %s for: %s", pname, query[:60])
            resp = self._search_with_retry(provider, query, max_results)
            return pname, resp

        if paid_providers:
            with ThreadPoolExecutor(max_workers=min(len(paid_providers), 4)) as pool:
                futures = {pool.submit(_try_provider, pp): pp[0] for pp in paid_providers}
                for future in as_completed(futures):
                    pname = futures[future]
                    try:
                        _, resp = future.result()
                        if resp.success and resp.results:
                            providers_used.append(pname)
                            for r in resp.results:
                                if r.url not in seen_urls:
                                    seen_urls.add(r.url)
                                    all_results.append(r)
                        else:
                            errors.append(f"{pname}: {resp.error_message}")
                    except Exception as e:
                        errors.append(f"{pname}: {e}")

        # 5. SearXNG — always trigger (no quota limits, mandatory)
        if self._has_keys("searxng"):
            searxng = self._get_provider("searxng")
            if searxng:
                logger.info("SearXNG search for: %s", query[:60])
                resp = self._search_with_retry(searxng, query, max_results)
                if resp.success and resp.results:
                    providers_used.append("searxng")
                    for r in resp.results:
                        if r.url not in seen_urls:
                            seen_urls.add(r.url)
                            all_results.append(r)
                else:
                    errors.append(f"searxng: {resp.error_message}")

        # 6. Build merged response
        if all_results:
            result = SearchResponse(
                query=query,
                results=all_results,  # return all results from all providers
                provider="+".join(providers_used),
                success=True,
                search_time=0.0,
            )
        else:
            result = SearchResponse(
                query=query,
                success=False,
                error_message="; ".join(errors) if errors else "No providers configured",
            )

        self._cache_put(ckey, result)
        self._dedup_complete(ckey, result)
        return result

    def search_stock_news(
        self, ticker: str, max_results: int = 10
    ) -> SearchResponse:
        """Convenience method: search for stock-specific news.

        Builds a query like ``"AAPL stock news"`` and delegates to ``search()``.
        """
        query = f"{ticker} stock news"
        return self.search(query, max_results)

    def search_global_news(
        self, topic: str = "stock market", max_results: int = 10
    ) -> SearchResponse:
        """Convenience method: search for macro/global market news."""
        return self.search(topic, max_results)


# ─── URL content extraction ─────────────────────────────────────────────────


def fetch_url_content(url: str, timeout: int = 5) -> str:
    """Fetch and extract the main article text from *url* using newspaper3k.

    Returns the article body truncated to 1500 characters.  On download or
    parse failure, returns an empty string (never raises).
    """
    try:
        from newspaper import Article

        article = Article(url)
        article.download()
        article.parse()
        text = article.text.strip()
        return text[:1500] if text else ""
    except Exception as e:
        logger.debug("fetch_url_content failed for %s: %s", url, e)
        return ""
