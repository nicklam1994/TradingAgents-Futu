"""Tiingo Market Data Provider — US equities via Tiingo API.

Requires TIINGO_API_KEY environment variable.
Placeholder provider — not yet implemented.
"""
from __future__ import annotations

import os
from .base import BaseMarketDataProvider


class TiingoProvider(BaseMarketDataProvider):
    """Tiingo data provider for US equities."""

    is_placeholder = True

    @property
    def name(self) -> str:
        return "tiingo"

    def _not_implemented(self, method: str) -> str:
        raise NotImplementedError(
            f"Provider 'tiingo' is a placeholder. {method} not yet implemented."
        )

    def _api_key(self) -> str | None:
        return os.getenv("TIINGO_API_KEY")

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        return self._not_implemented("get_stock_data")

    def get_indicators(self, symbol: str, indicator: str, curr_date: str, look_back_days: int) -> str:
        return self._not_implemented("get_indicators")

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        return self._not_implemented("get_fundamentals")

    def get_balance_sheet(self, ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
        return self._not_implemented("get_balance_sheet")

    def get_cashflow(self, ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
        return self._not_implemented("get_cashflow")

    def get_income_statement(self, ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
        return self._not_implemented("get_income_statement")

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        return self._not_implemented("get_news")

    def get_global_news(self, curr_date: str, look_back_days: int = 7, limit: int = 50) -> str:
        return self._not_implemented("get_global_news")

    def get_insider_transactions(self, symbol: str) -> str:
        return self._not_implemented("get_insider_transactions")
