from typing import Dict

from .base import BaseMarketDataProvider
from .futu_provider import FutuProvider
from .yfinance_provider import YFinanceProvider
from .alpha_vantage_provider import AlphaVantageProvider
from .search_news_provider import SearchNewsProvider


class DataProviderRegistry:
    """Simple in-memory provider registry."""

    def __init__(self):
        self._providers: Dict[str, BaseMarketDataProvider] = {}

    def register(self, provider: BaseMarketDataProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, provider_name: str) -> BaseMarketDataProvider | None:
        return self._providers.get(provider_name)

    def list_names(self) -> list[str]:
        return list(self._providers.keys())


def build_default_registry() -> DataProviderRegistry:
    registry = DataProviderRegistry()
    # Futu has highest priority for US/HK equities
    registry.register(FutuProvider())
    registry.register(YFinanceProvider())
    registry.register(AlphaVantageProvider())
    # Search-based news provider (web search fallback)
    registry.register(SearchNewsProvider())
    return registry
