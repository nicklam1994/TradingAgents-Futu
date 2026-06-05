"""LangChain tool for social sentiment data (Reddit/X/Polymarket).

Provides a ``get_social_sentiment`` tool that fetches social-media sentiment
intelligence from the SocialSentimentService (api.adanos.org) and returns
a formatted text block suitable for LLM analyst prompts.
"""

from langchain_core.tools import tool
from typing import Annotated


@tool
def get_social_sentiment(
    symbol: Annotated[str, "Stock ticker symbol, e.g. TSLA, AAPL"],
) -> str:
    """Fetch social-media sentiment intelligence for a stock from Reddit, X (Twitter), and Polymarket.

    Returns sentiment scores, buzz levels, mention volumes, and trending data
    aggregated from social platforms. Useful for gauging retail investor mood,
    viral momentum, and prediction market pricing for a given ticker.

    Only works for US stock tickers. Returns a formatted text report.
    """
    from tradingagents.dataflows.social_sentiment import SocialSentimentService

    svc = SocialSentimentService.from_env()
    if not svc.is_available:
        return "Social sentiment data unavailable: SOCIAL_SENTIMENT_API_KEY not configured."

    context = svc.get_social_context(symbol)
    if context is None:
        return f"No social sentiment data found for {symbol}."

    return context
