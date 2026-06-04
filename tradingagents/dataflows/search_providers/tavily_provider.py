"""Tavily Search Provider — web search via tavily-python SDK.

Requires TAVILY_API_KEYS environment variable (comma-separated for rotation).
Tavily is optimized for LLM consumption and returns high-quality summaries.
"""

import logging
import time

from ..search_service import SearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)


class TavilySearchProvider(SearchProvider):
    """Search provider backed by the Tavily Search API."""

    @property
    def name(self) -> str:
        return "tavily"

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        keys = self._keys("TAVILY_API_KEYS")
        if not keys:
            return SearchResponse(
                query=query,
                success=False,
                provider=self.name,
                error_message="No TAVILY_API_KEYS configured",
            )

        # Use first available key (rotation handled by SearchService)
        api_key = keys[0]
        t0 = time.monotonic()

        try:
            from tavily import TavilyClient

            client = TavilyClient(api_key=api_key)
            response = client.search(query=query, max_results=max_results)

            results = []
            for item in response.get("results", []):
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        snippet=item.get("content", ""),
                        url=item.get("url", ""),
                        source="tavily",
                        published_date=item.get("published_date", ""),
                    )
                )

            return SearchResponse(
                query=query,
                results=results,
                provider=self.name,
                success=True,
                search_time=time.monotonic() - t0,
            )
        except Exception as e:
            logger.warning("Tavily search failed: %s", e)
            return SearchResponse(
                query=query,
                provider=self.name,
                success=False,
                error_message=str(e),
                search_time=time.monotonic() - t0,
            )
