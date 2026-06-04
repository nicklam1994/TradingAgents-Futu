"""Futu Provider — US/HK market data via FutuOpenD API.

Requires a running FutuOpenD instance (default: 127.0.0.1:11111).
Configure via FUTU_OPEND_HOST / FUTU_OPEND_PORT environment variables.
"""

import os

from .base import BaseMarketDataProvider


class FutuProvider(BaseMarketDataProvider):
    """Market data provider backed by Futu OpenD API.

    Supports US and HK equities. A-share (SH/SZ) symbols are not supported
    by Futu and will raise NotImplementedError.
    """

    @property
    def name(self) -> str:
        return "futu"

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        raise NotImplementedError

    def get_indicators(
        self, symbol: str, indicator: str, curr_date: str, look_back_days: int
    ) -> str:
        raise NotImplementedError

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        raise NotImplementedError

    def get_balance_sheet(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        raise NotImplementedError

    def get_cashflow(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        raise NotImplementedError

    def get_income_statement(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        raise NotImplementedError

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        raise NotImplementedError

    def get_global_news(
        self, curr_date: str, look_back_days: int = 7, limit: int = 50
    ) -> str:
        raise NotImplementedError

    def get_insider_transactions(self, symbol: str) -> str:
        raise NotImplementedError

    def get_realtime_quotes(self, symbols: list[str]) -> str:
        raise NotImplementedError
