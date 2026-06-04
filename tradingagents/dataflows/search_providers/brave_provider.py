"""Brave Search Provider — web search via Brave Search REST API.

Requires BRAVE_API_KEYS environment variable (comma-separated for rotation).
Free tier: 2,000 queries/month. No SDK dependency — pure HTTP.
"""

import json
import logging
import time
import urllib.parse
import urllib.request

from ..search_service import SearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchProvider(SearchProvider):
    """Search provider backed by the Brave Search API."""

    @property
    def name(self) -> str:
        return "brave"

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        keys = self._keys("BRAVE_API_KEYS")
        if not keys:
            return SearchResponse(
                query=query,
                success=False,
                provider=self.name,
                error_message="No BRAVE_API_KEYS configured",
            )

        api_key = keys[0]
        t0 = time.monotonic()

        try:
            url = f"{_BRAVE_ENDPOINT}?q={urllib.parse.quote(query)}&count={max_results}"
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            results = []
            for item in data.get("web", {}).get("results", []):
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        snippet=item.get("description", ""),
                        url=item.get("url", ""),
                        source="brave",
                        published_date=item.get("page_age", ""),
                    )
                )

            return SearchResponse(
                query=query,
                results=results[:max_results],
                provider=self.name,
                success=True,
                search_time=time.monotonic() - t0,
            )
        except Exception as e:
            logger.warning("Brave search failed: %s", e)
            return SearchResponse(
                query=query,
                provider=self.name,
                success=False,
                error_message=str(e),
                search_time=time.monotonic() - t0,
            )
