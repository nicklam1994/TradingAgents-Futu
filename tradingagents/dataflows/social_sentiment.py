# -*- coding: utf-8 -*-
"""Social Sentiment Intelligence Service — Reddit / X / Polymarket.

Fetches social-media sentiment data from api.adanos.org and formats
it as a text block suitable for injection into LLM analysis prompts.

Optional — requires SOCIAL_SENTIMENT_API_KEY environment variable.
Only activates for US stock codes (AAPL, TSLA, etc.).

Ported from daily_stock_analysis SocialSentimentService with adaptations
for TradingAgents' provider style (tenacity retry, TTL cache, thread-safe).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ─── Retry / timeout constants ─────────────────────────────────────────────

_TRANSIENT_EXCEPTIONS = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

_REQUEST_TIMEOUT = 8          # seconds per HTTP request
_REQUEST_RETRY_ATTEMPTS = 2   # tenacity max attempts
_REQUEST_RETRY_WAIT_CAP = 5   # wait_exponential max seconds

# ─── Retry-enabled GET ─────────────────────────────────────────────────────


@retry(
    stop=stop_after_attempt(_REQUEST_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=1, max=_REQUEST_RETRY_WAIT_CAP),
    retry=retry_if_exception_type(_TRANSIENT_EXCEPTIONS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _get_with_retry(
    url: str,
    *,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None,
    timeout: int = _REQUEST_TIMEOUT,
) -> requests.Response:
    """GET with tenacity retry on transient network errors."""
    return requests.get(url, headers=headers, params=params or {}, timeout=timeout)


# ─── Service ───────────────────────────────────────────────────────────────


class SocialSentimentService:
    """Social Sentiment Intelligence — Reddit / X / Polymarket.

    Fetches social-media sentiment data from api.adanos.org and formats
    it as a prompt-ready text block for LLM analysts.

    Usage::

        svc = SocialSentimentService()  # reads env vars automatically
        if svc.is_available:
            context = svc.get_social_context("TSLA")
    """

    # Cache TTL for trending endpoints (seconds)
    _TRENDING_CACHE_TTL = 600  # 10 minutes

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: str = "https://api.adanos.org",
    ):
        self._api_key = (api_key or "").strip() or None
        self._api_url = (api_url or "https://api.adanos.org").rstrip("/")
        # Simple in-memory cache: {"key": (timestamp, data)}
        self._cache: Dict[str, tuple] = {}
        self._cache_lock = threading.RLock()
        self._cache_inflight: Dict[str, threading.Event] = {}

    @classmethod
    def from_env(cls) -> "SocialSentimentService":
        """Create instance from environment variables."""
        return cls(
            api_key=os.getenv("SOCIAL_SENTIMENT_API_KEY"),
            api_url=os.getenv("SOCIAL_SENTIMENT_BASE_URL", "https://api.adanos.org"),
        )

    @property
    def is_available(self) -> bool:
        """True if API key is configured."""
        return self._api_key is not None

    @property
    def _headers(self) -> Dict[str, str]:
        return {"X-API-Key": self._api_key or "", "Accept": "application/json"}

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _fetch_json(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict]:
        """Fetch JSON from API, return None on any error."""
        try:
            resp = _get_with_retry(url, headers=self._headers, params=params)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("Social sentiment API %s returned %s", url, resp.status_code)
        except _TRANSIENT_EXCEPTIONS as exc:
            logger.warning("Social sentiment API %s network error: %s", url, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Social sentiment API %s unexpected error: %s", url, exc)
        return None

    # ------------------------------------------------------------------
    # Cache layer (same pattern as SearchService)
    # ------------------------------------------------------------------

    @classmethod
    def _cache_wait_timeout_seconds(cls) -> float:
        """Compute a safe wait timeout for inflight dedup."""
        request_budget = (
            _REQUEST_TIMEOUT * _REQUEST_RETRY_ATTEMPTS
        ) + _REQUEST_RETRY_WAIT_CAP
        return max(1.0, min(float(cls._TRENDING_CACHE_TTL), float(request_budget), 30.0))

    def _fetch_cached(
        self,
        cache_key: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """Fetch with TTL cache and inflight dedup (for trending endpoints).

        Follows the same dedup-or-acquire pattern as SearchService:
        - First caller for a key becomes the owner, fetches data.
        - Concurrent callers wait on the owner's Event, then read from cache.
        - After TTL expires, the next caller becomes the new owner.
        """
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and (now - cached[0]) < self._TRENDING_CACHE_TTL:
                return cached[1]
            inflight = self._cache_inflight.get(cache_key)
            if inflight is None:
                inflight = threading.Event()
                self._cache_inflight[cache_key] = inflight
                owner = True
            else:
                owner = False

        if not owner:
            # Wait for the owner to finish, then read from cache
            inflight.wait(timeout=self._cache_wait_timeout_seconds())
            now = time.monotonic()
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached and (now - cached[0]) < self._TRENDING_CACHE_TTL:
                    return cached[1]
            # Cache still empty after wait — fall through and fetch ourselves
            data = self._fetch_json(url, params)
            if data is not None:
                with self._cache_lock:
                    self._cache[cache_key] = (time.monotonic(), data)
            return data

        # Owner path: fetch, cache, signal waiters
        try:
            data = self._fetch_json(url, params)
            if data is not None:
                with self._cache_lock:
                    self._cache[cache_key] = (time.monotonic(), data)
            return data
        finally:
            with self._cache_lock:
                current = self._cache_inflight.get(cache_key)
                if current is inflight:
                    self._cache_inflight.pop(cache_key, None)
                    inflight.set()

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def fetch_reddit_report(self, ticker: str) -> Optional[Dict]:
        """Fetch detailed Reddit report for a single ticker."""
        url = f"{self._api_url}/reddit/stocks/v1/report/{ticker.upper()}"
        return self._fetch_json(url)

    def fetch_reddit_trending(self) -> Optional[List[Dict]]:
        """Fetch Reddit trending stocks (cached 10min)."""
        url = f"{self._api_url}/reddit/stocks/v1/trending"
        data = self._fetch_cached("reddit_trending", url)
        if isinstance(data, dict):
            return data.get("trending", data.get("data", []))
        if isinstance(data, list):
            return data
        return None

    def fetch_x_trending(self) -> Optional[List[Dict]]:
        """Fetch X/Twitter trending stocks (cached 10min)."""
        url = f"{self._api_url}/x/stocks/v1/trending"
        data = self._fetch_cached("x_trending", url)
        if isinstance(data, dict):
            return data.get("trending", data.get("data", []))
        if isinstance(data, list):
            return data
        return None

    def fetch_polymarket_trending(self) -> Optional[List[Dict]]:
        """Fetch Polymarket trending stocks (cached 10min)."""
        url = f"{self._api_url}/polymarket/stocks/v1/trending"
        data = self._fetch_cached("polymarket_trending", url)
        if isinstance(data, dict):
            return data.get("trending", data.get("data", []))
        if isinstance(data, list):
            return data
        return None

    # ------------------------------------------------------------------
    # Main entry points
    # ------------------------------------------------------------------

    def get_social_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Get structured sentiment data for a symbol.

        Returns a dict with keys: reddit, x, polymarket, available.
        Each platform entry contains raw API data or None.
        """
        if not self.is_available:
            return {"available": False, "reddit": None, "x": None, "polymarket": None}

        ticker = symbol.upper()

        # Reddit per-ticker report (richest data)
        reddit_data = self.fetch_reddit_report(ticker)

        # X trending — filter for this ticker
        x_entry = None
        x_trending = self.fetch_x_trending()
        if x_trending:
            x_entry = self._find_ticker_in_trending(x_trending, ticker)

        # Polymarket trending — filter for this ticker
        poly_entry = None
        poly_trending = self.fetch_polymarket_trending()
        if poly_trending:
            poly_entry = self._find_ticker_in_trending(poly_trending, ticker)

        return {
            "available": True,
            "reddit": reddit_data,
            "x": x_entry,
            "polymarket": poly_entry,
        }

    def get_social_context(self, symbol: str) -> Optional[str]:
        """Fetch social sentiment from all platforms and return a formatted
        text block for the LLM prompt.  Returns None if no data found.
        """
        if not self.is_available:
            return None

        ticker = symbol.upper()
        data = self.get_social_sentiment(ticker)

        reddit_data = data.get("reddit")
        x_entry = data.get("x")
        poly_entry = data.get("polymarket")

        if not reddit_data and not x_entry and not poly_entry:
            return None

        return self._format_social_intel(ticker, reddit_data, x_entry, poly_entry)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_ticker_in_trending(
        trending: List[Dict], ticker: str
    ) -> Optional[Dict]:
        """Find a ticker entry in a trending list."""
        for entry in trending:
            code = (
                entry.get("ticker")
                or entry.get("symbol")
                or entry.get("code")
                or ""
            ).upper()
            if code == ticker:
                return entry
        return None

    @staticmethod
    def _coalesce(*values: Any) -> Any:
        """Return the first value that is not None (preserves 0 and 0.0)."""
        for v in values:
            if v is not None:
                return v
        return None

    @staticmethod
    def _format_social_intel(
        ticker: str,
        reddit_data: Optional[Dict],
        x_entry: Optional[Dict],
        poly_entry: Optional[Dict],
    ) -> str:
        """Format social sentiment data as a prompt-ready text block."""
        lines = [
            f"📱 Social Sentiment Intelligence for {ticker} "
            f"(Reddit / X / Polymarket)"
        ]
        lines.append("=" * 60)

        # --- Reddit ---
        if reddit_data:
            lines.append("\n🔴 Reddit Community Sentiment:")
            report = reddit_data.get("report", reddit_data)

            buzz = SocialSentimentService._coalesce(
                report.get("buzz_score"), report.get("buzz")
            )
            if buzz is not None:
                trend_label = report.get("trend", "")
                lines.append(
                    f"  Buzz Score: {buzz}/100 ({trend_label})"
                    if trend_label
                    else f"  Buzz Score: {buzz}/100"
                )

            sentiment = SocialSentimentService._coalesce(
                report.get("sentiment_score"), report.get("sentiment")
            )
            if sentiment is not None:
                lines.append(f"  Sentiment Score: {sentiment}")

            mentions = SocialSentimentService._coalesce(
                report.get("total_mentions"), report.get("mentions")
            )
            if mentions is not None:
                subs = SocialSentimentService._coalesce(
                    report.get("subreddit_count"), report.get("subreddits")
                )
                sub_str = f" across {subs} subreddits" if subs else ""
                lines.append(f"  Mentions: {mentions}{sub_str} (7-day)")

            top_mentions = report.get("top_mentions", [])
            if top_mentions:
                lines.append("  Top Mentions:")
                for i, m in enumerate(top_mentions[:5], 1):
                    text = (m.get("text") or m.get("title") or "")[:120]
                    sub = m.get("subreddit", "")
                    score = SocialSentimentService._coalesce(
                        m.get("sentiment_score"), m.get("sentiment")
                    )
                    upvotes = m.get("upvotes", "")
                    meta_parts = []
                    if score is not None:
                        meta_parts.append(f"sentiment: {score}")
                    if sub:
                        meta_parts.append(f"r/{sub}")
                    if upvotes:
                        meta_parts.append(f"{upvotes} upvotes")
                    meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
                    lines.append(f'    {i}. "{text}"{meta}')

            daily = report.get("daily_stats", [])
            if daily:
                lines.append("  Recent Daily Activity:")
                for d in daily[:5]:
                    day = d.get("date", "")
                    day_mentions = d.get("mentions", "?")
                    day_sentiment = d.get("avg_sentiment", "?")
                    lines.append(
                        f"    {day}: {day_mentions} mentions, "
                        f"avg sentiment {day_sentiment}"
                    )
        else:
            lines.append("\n🔴 Reddit: No data available")

        # --- X / Twitter ---
        if x_entry:
            lines.append("\n🐦 X (Twitter) Sentiment:")
            x_buzz = SocialSentimentService._coalesce(
                x_entry.get("buzz_score"), x_entry.get("buzz")
            )
            x_sentiment = SocialSentimentService._coalesce(
                x_entry.get("sentiment_score"), x_entry.get("sentiment")
            )
            x_mentions = SocialSentimentService._coalesce(
                x_entry.get("total_mentions"), x_entry.get("mentions")
            )
            x_trend = x_entry.get("trend", "")
            if x_buzz is not None:
                lines.append(
                    f"  Buzz Score: {x_buzz}/100 ({x_trend})"
                    if x_trend
                    else f"  Buzz Score: {x_buzz}/100"
                )
            if x_sentiment is not None:
                lines.append(f"  Sentiment Score: {x_sentiment}")
            if x_mentions is not None:
                lines.append(f"  Mentions: {x_mentions} (7-day)")
        else:
            lines.append("\n🐦 X (Twitter): No data available")

        # --- Polymarket ---
        if poly_entry:
            lines.append("\n🔮 Polymarket (Prediction Markets):")
            poly_buzz = SocialSentimentService._coalesce(
                poly_entry.get("buzz_score"), poly_entry.get("buzz")
            )
            poly_sentiment = SocialSentimentService._coalesce(
                poly_entry.get("sentiment_score"), poly_entry.get("sentiment")
            )
            poly_trades = SocialSentimentService._coalesce(
                poly_entry.get("trade_count"), poly_entry.get("trades")
            )
            if poly_buzz is not None:
                lines.append(f"  Buzz Score: {poly_buzz}/100")
            if poly_sentiment is not None:
                lines.append(f"  Market Sentiment: {poly_sentiment}")
            if poly_trades is not None:
                lines.append(f"  Trade Count: {poly_trades}")
        else:
            lines.append("\n🔮 Polymarket: No active prediction markets found")

        lines.append("")
        lines.append("Source: api.adanos.org — Real-time social sentiment aggregation")
        return "\n".join(lines)
