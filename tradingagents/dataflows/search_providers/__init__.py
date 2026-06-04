"""Search engine providers — concrete implementations of SearchProvider ABC.

Each provider wraps a different search API:
  - TavilySearchProvider   (tavily-python SDK)
  - BraveSearchProvider     (Brave Search REST API)
  - SerpAPISearchProvider   (SerpAPI SDK)
  - SearXNGSearchProvider   (self-hosted SearXNG REST API)
  - BochaSearchProvider     (Bocha Search API)
  - AnspireSearchProvider   (Anspire Search API)
  - MiniMaxSearchProvider   (MiniMax Coding Plan API)
"""

from .tavily_provider import TavilySearchProvider
from .brave_provider import BraveSearchProvider
from .serpapi_provider import SerpAPISearchProvider
from .searxng_provider import SearXNGSearchProvider
from .bocha_provider import BochaSearchProvider
from .anspire_provider import AnspireSearchProvider
from .minimax_provider import MiniMaxSearchProvider

__all__ = [
    "TavilySearchProvider",
    "BraveSearchProvider",
    "SerpAPISearchProvider",
    "SearXNGSearchProvider",
    "BochaSearchProvider",
    "AnspireSearchProvider",
    "MiniMaxSearchProvider",
]
