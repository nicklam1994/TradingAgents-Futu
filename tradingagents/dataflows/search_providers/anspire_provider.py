"""Anspire Search Provider — web search via Anspire Search API.

Requires ANSPIRE_API_KEYS environment variable (comma-separated for rotation).
Anspire provides Chinese-language optimized search results with financial focus.
"""

import json
import logging
import time
import urllib.request

from ..search_service import SearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_ANSPIRE_ENDPOINT = "https://api.anspire.cn/v1/search"


class AnspireSearchProvider(SearchProvider):
    """Search provider backed by Anspire Search API."""

    @property
    def name(self) -> str:
        return "anspire"

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        keys = self._keys("ANSPIRE_API_KEYS")
        if not keys:
            return SearchResponse(
                query=query,
                success=False,
                provider=self.name,
                error_message="No ANSPIRE_API_KEYS configured",
            )

        api_key = keys[0]
        t0 = time.monotonic()

        try:
            payload = json.dumps({
                "query": query,
                "count": max_results,
            }).encode()
            req = urllib.request.Request(
                _ANSPIRE_ENDPOINT,
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
            # Anspire returns results under data.results
            for item in data.get("data", {}).get("results", []):
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                        url=item.get("url", ""),
                        source="anspire",
                        published_date=item.get("published_date", ""),
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
            logger.warning("Anspire search failed: %s", e)
            return SearchResponse(
                query=query,
                provider=self.name,
                success=False,
                error_message=str(e),
                search_time=time.monotonic() - t0,
            )
