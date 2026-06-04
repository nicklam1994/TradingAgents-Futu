"""Bocha Search Provider — web search via Bocha Search API.

Requires BOCHA_API_KEYS environment variable (comma-separated for rotation).
Bocha is a Chinese search API optimized for financial and news content.
"""

import json
import logging
import time
import urllib.request

from ..search_service import SearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_BOCHA_ENDPOINT = "https://api.bochaai.com/v1/web-search"


class BochaSearchProvider(SearchProvider):
    """Search provider backed by Bocha Search API."""

    @property
    def name(self) -> str:
        return "bocha"

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        keys = self._keys("BOCHA_API_KEYS")
        if not keys:
            return SearchResponse(
                query=query,
                success=False,
                provider=self.name,
                error_message="No BOCHA_API_KEYS configured",
            )

        api_key = keys[0]
        t0 = time.monotonic()

        try:
            payload = json.dumps({
                "query": query,
                "freshness": "noLimit",
                "summary": True,
                "count": max_results,
            }).encode()
            req = urllib.request.Request(
                _BOCHA_ENDPOINT,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            results = []
            # Bocha returns results under data.webPages.value
            web_pages = data.get("data", {}).get("webPages", {}).get("value", [])
            for item in web_pages:
                results.append(
                    SearchResult(
                        title=item.get("name", ""),
                        snippet=item.get("snippet", ""),
                        url=item.get("url", ""),
                        source="bocha",
                        published_date=item.get("dateLastCrawled", ""),
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
            logger.warning("Bocha search failed: %s", e)
            return SearchResponse(
                query=query,
                provider=self.name,
                success=False,
                error_message=str(e),
                search_time=time.monotonic() - t0,
            )
