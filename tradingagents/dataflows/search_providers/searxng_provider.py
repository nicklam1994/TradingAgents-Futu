"""SearXNG Search Provider — meta-search via self-hosted SearXNG instances.

Requires SEARXNG_BASE_URLS environment variable (comma-separated instance URLs).
SearXNG is a free, privacy-respecting metasearch engine — no API key needed,
just a running instance (e.g. http://localhost:8888).
"""

import json
import logging
import time
import urllib.parse
import urllib.request

from ..search_service import SearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)


class SearXNGSearchProvider(SearchProvider):
    """Search provider backed by self-hosted SearXNG instances."""

    @property
    def name(self) -> str:
        return "searxng"

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        urls = self._keys("SEARXNG_BASE_URLS")
        if not urls:
            return SearchResponse(
                query=query,
                success=False,
                provider=self.name,
                error_message="No SEARXNG_BASE_URLS configured",
            )

        base_url = urls[0].rstrip("/")
        t0 = time.monotonic()

        try:
            url = (
                f"{base_url}/search"
                f"?q={urllib.parse.quote(query)}"
                f"&format=json&pageno=1"
            )
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            results = []
            for item in data.get("results", []):
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        snippet=item.get("content", ""),
                        url=item.get("url", ""),
                        source="searxng",
                        published_date=item.get("publishedDate", ""),
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
            logger.warning("SearXNG search failed: %s", e)
            return SearchResponse(
                query=query,
                provider=self.name,
                success=False,
                error_message=str(e),
                search_time=time.monotonic() - t0,
            )
