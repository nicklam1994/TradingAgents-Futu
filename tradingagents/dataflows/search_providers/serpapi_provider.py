"""SerpAPI Search Provider — Google search results via SerpAPI SDK.

Requires SERPAPI_API_KEYS environment variable (comma-separated for rotation).
SerpAPI provides structured Google results; free tier: 100 searches/month.
"""

import logging
import time

from ..search_service import SearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)


class SerpAPISearchProvider(SearchProvider):
    """Search provider backed by SerpAPI (Google results)."""

    @property
    def name(self) -> str:
        return "serpapi"

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        keys = self._keys("SERPAPI_API_KEYS")
        if not keys:
            return SearchResponse(
                query=query,
                success=False,
                provider=self.name,
                error_message="No SERPAPI_API_KEYS configured",
            )

        api_key = keys[0]
        t0 = time.monotonic()

        try:
            from serpapi import Client

            params = {
                "q": query,
                "num": max_results,
                "api_key": api_key,
                "engine": "google",
            }
            client = Client(api_key=api_key)
            data = client.search(params)

            results = []
            for item in data.get("organic_results", []):
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                        url=item.get("link", ""),
                        source="serpapi",
                        published_date=item.get("date", ""),
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
            logger.warning("SerpAPI search failed: %s", e)
            return SearchResponse(
                query=query,
                provider=self.name,
                success=False,
                error_message=str(e),
                search_time=time.monotonic() - t0,
            )
