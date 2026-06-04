"""MiniMax Search Provider — web search via MiniMax Coding Plan API.

Requires MINIMAX_API_KEYS environment variable (comma-separated for rotation).
MiniMax provides a coding-plan search endpoint optimized for technical content.
"""

import json
import logging
import time
import urllib.request

from ..search_service import SearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_MINIMAX_ENDPOINT = "https://api.minimax.chat/v1/text/chatcompletion_v2"


class MiniMaxSearchProvider(SearchProvider):
    """Search provider backed by MiniMax Coding Plan API."""

    @property
    def name(self) -> str:
        return "minimax"

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        keys = self._keys("MINIMAX_API_KEYS")
        if not keys:
            return SearchResponse(
                query=query,
                success=False,
                provider=self.name,
                error_message="No MINIMAX_API_KEYS configured",
            )

        api_key = keys[0]
        t0 = time.monotonic()

        try:
            # MiniMax uses a chat completion endpoint with web search enabled
            payload = json.dumps({
                "model": "MiniMax-Text-01",
                "messages": [
                    {"role": "user", "content": f"搜索并总结：{query}"}
                ],
                "tools": [{"type": "web_search"}],
                "max_tokens": 2048,
            }).encode()
            req = urllib.request.Request(
                _MINIMAX_ENDPOINT,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())

            results = []
            # Extract search results from the response
            # MiniMax returns search results in the tool_calls or content
            choices = data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                # Check for web search tool results
                tool_calls = message.get("tool_calls", [])
                for tc in tool_calls:
                    func = tc.get("function", {})
                    if func.get("name") == "web_search":
                        args = json.loads(func.get("arguments", "{}"))
                        for item in args.get("results", []):
                            results.append(
                                SearchResult(
                                    title=item.get("title", ""),
                                    snippet=item.get("snippet", ""),
                                    url=item.get("url", ""),
                                    source="minimax",
                                    published_date=item.get("date", ""),
                                )
                            )

                # If no tool results, use the content as a single result
                if not results and message.get("content"):
                    results.append(
                        SearchResult(
                            title=f"Search: {query}",
                            snippet=message["content"][:500],
                            url="",
                            source="minimax",
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
            logger.warning("MiniMax search failed: %s", e)
            return SearchResponse(
                query=query,
                provider=self.name,
                success=False,
                error_message=str(e),
                search_time=time.monotonic() - t0,
            )
