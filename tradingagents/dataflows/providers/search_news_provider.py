"""Search News Provider — news data via web search engines.

This provider bridges the ``SearchService`` (7-engine search orchestrator) into
the ``BaseMarketDataProvider`` interface so the news routing can use web search
as a fallback when native APIs (yfinance, akshare) don't cover a ticker or
region.
"""

import json
import logging
from datetime import datetime, timedelta

from ..search_service import SearchService, fetch_url_content
from .base import BaseMarketDataProvider

logger = logging.getLogger(__name__)

# Module-level singleton — shared across all call sites to leverage cache
_search_service: SearchService | None = None


def _get_search_service() -> SearchService:
    """Return or create the module-level SearchService singleton."""
    global _search_service
    if _search_service is None:
        _search_service = SearchService()
    return _search_service


class SearchNewsProvider(BaseMarketDataProvider):
    """Market data provider that fetches news via web search engines.

    Only ``get_news()`` and ``get_global_news()`` are implemented; all other
    data methods raise ``NotImplementedError`` so the fallback chain skips to
    the next provider.
    """

    @property
    def name(self) -> str:
        return "search_news"

    # ── News methods (implemented) ───────────────────────────────────────────

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        """Fetch stock-specific news via web search.

        Searches for recent news about *ticker*, optionally enriches top
        results with full article text via ``fetch_url_content()``.
        """
        svc = _get_search_service()
        resp = svc.search_stock_news(ticker, max_results=10)

        if not resp.success or not resp.results:
            return f"No search news found for {ticker}"

        lines = [f"## {ticker} 新闻（via {resp.provider}）\n"]
        for i, r in enumerate(resp.results, 1):
            content = ""
            if r.url:
                content = fetch_url_content(r.url)
            lines.append(f"### {i}. {r.title}")
            if r.published_date:
                lines.append(f"**日期**: {r.published_date}")
            lines.append(f"**来源**: {r.source} | {r.url}")
            lines.append(f"**摘要**: {r.snippet}")
            if content:
                lines.append(f"**正文**: {content[:800]}")
            lines.append("")

        return "\n".join(lines)

    def get_global_news(
        self, curr_date: str, look_back_days: int = 7, limit: int = 50
    ) -> str:
        """Fetch macro/global market news via web search."""
        svc = _get_search_service()
        resp = svc.search_global_news("stock market news today", max_results=10)

        if not resp.success or not resp.results:
            return "No global search news found"

        lines = [f"## 全球市场新闻（via {resp.provider}）\n"]
        for i, r in enumerate(resp.results, 1):
            lines.append(f"### {i}. {r.title}")
            if r.published_date:
                lines.append(f"**日期**: {r.published_date}")
            lines.append(f"**来源**: {r.source} | {r.url}")
            lines.append(f"**摘要**: {r.snippet}")
            lines.append("")

        return "\n".join(lines)

    # ── Other data methods (not supported) ───────────────────────────────────

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        raise NotImplementedError("SearchNewsProvider does not provide stock data")

    def get_indicators(
        self, symbol: str, indicator: str, curr_date: str, look_back_days: int
    ) -> str:
        raise NotImplementedError("SearchNewsProvider does not provide indicators")

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        raise NotImplementedError("SearchNewsProvider does not provide fundamentals")

    def get_balance_sheet(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        raise NotImplementedError("SearchNewsProvider does not provide balance sheet")

    def get_cashflow(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        raise NotImplementedError("SearchNewsProvider does not provide cashflow")

    def get_income_statement(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        raise NotImplementedError(
            "SearchNewsProvider does not provide income statement"
        )

    def get_insider_transactions(self, symbol: str) -> str:
        raise NotImplementedError(
            "SearchNewsProvider does not provide insider transactions"
        )
