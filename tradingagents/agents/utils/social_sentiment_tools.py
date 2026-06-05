"""LangChain tools for social sentiment data (Reddit / X / Polymarket).

These tools wrap SocialSentimentService and are designed to be used by
LangChain agents (e.g., the Social Media Analyst).
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Lazy-loaded singleton — avoids import-time failures when API key is absent
_svc = None
_svc_lock = __import__("threading").Lock()


def _get_service():
    """Return a cached SocialSentimentService instance (created on first use)."""
    global _svc  # noqa: PLW0603
    if _svc is None:
        with _svc_lock:
            if _svc is None:
                from tradingagents.dataflows.social_sentiment import (
                    SocialSentimentService,
                )
                _svc = SocialSentimentService.from_env()
    return _svc


@tool
def get_social_sentiment(
    symbol: Annotated[str, "Stock ticker symbol (e.g. TSLA, AAPL)"],
) -> str:
    """Retrieve social media sentiment data for a stock ticker.

    Aggregates sentiment from Reddit, X (Twitter), and Polymarket.
    Returns buzz scores, sentiment scores, mention counts, and trending
    context from social platforms. Useful for gauging retail investor
    mood and viral stock narratives.

    Args:
        symbol: Stock ticker symbol (e.g. TSLA, AAPL, NVDA)

    Returns:
        str: Formatted social sentiment analysis text, or a message
             indicating data is unavailable.
    """
    svc = _get_service()
    if not svc.is_available:
        return (
            "Social sentiment data unavailable: SOCIAL_SENTIMENT_API_KEY "
            "is not configured. Set it in .env to enable Reddit/X/Polymarket "
            "sentiment tracking."
        )

    context = svc.get_social_context(symbol)
    if context:
        return context

    return (
        f"No social sentiment data found for {symbol.upper()} on any "
        f"platform (Reddit, X, Polymarket). The ticker may not be "
        f"actively discussed or the API returned no results."
    )
