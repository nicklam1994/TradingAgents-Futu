"""
Trading constants and enums for TradingAgents-Futu.

Standardizes hardcoded strings like "buy"/"sell", "HK"/"US", etc.
Based on vnpy/trader/constant.py patterns, adapted for TAF.

Phase 13.1: Constant enum standardization
"""
from __future__ import annotations

from enum import Enum


class Direction(Enum):
    """Trading direction."""
    LONG = "long"           # 买入/做多
    SHORT = "short"         # 卖出/做空
    NET = "net"             # 净头寸

    @property
    def cn(self) -> str:
        """Chinese display name."""
        _map = {"long": "买入", "short": "卖出", "net": "净头寸"}
        return _map[self.value]


class OrderStatus(Enum):
    """Order status."""
    SUBMITTING = "submitting"       # 提交中
    NOTTRADED = "nottraded"         # 未成交
    PARTTRADED = "parttraded"       # 部分成交
    ALLTRADED = "alltraded"         # 全部成交
    CANCELLED = "cancelled"         # 已撤销
    REJECTED = "rejected"           # 已拒绝
    EXPIRED = "expired"             # 已过期

    @property
    def cn(self) -> str:
        """Chinese display name."""
        _map = {
            "submitting": "提交中", "nottraded": "未成交",
            "parttraded": "部分成交", "alltraded": "全部成交",
            "cancelled": "已撤销", "rejected": "已拒绝", "expired": "已过期",
        }
        return _map[self.value]


class OrderType(Enum):
    """Order type."""
    LIMIT = "limit"         # 限价单
    MARKET = "market"       # 市价单
    STOP = "stop"           # 止损单
    STOP_LIMIT = "stop_limit"  # 止损限价单
    FAK = "fak"             # Fill and Kill
    FOK = "fok"             # Fill or Kill


class Exchange(Enum):
    """Exchange identifier.

    Covers HK, US, CN, and major global exchanges.
    Based on vnpy Exchange enum, trimmed to TAF-relevant markets.
    """
    # Hong Kong
    SEHK = "SEHK"           # 港交所
    HKFE = "HKFE"           # 港交所期货

    # United States
    NYSE = "NYSE"           # 纽约证券交易所
    NASDAQ = "NASDAQ"       # 纳斯达克
    AMEX = "AMEX"           # 美交所
    CBOE = "CBOE"           # 芝加哥期权交易所
    CME = "CME"             # 芝加哥商品交易所

    # China (A-share)
    SSE = "SSE"             # 上交所
    SZSE = "SZSE"           # 深交所
    SHFE = "SHFE"           # 上期所
    DCE = "DCE"             # 大商所
    CZCE = "CZCE"           # 郑商所
    CFFEX = "CFFEX"         # 中金所

    # Crypto
    BINANCE = "BINANCE"
    OKEX = "OKEX"
    HUOBI = "HUOBI"

    @property
    def cn(self) -> str:
        """Chinese display name."""
        _map = {
            "SEHK": "港交所", "HKFE": "港交所期货",
            "NYSE": "纽交所", "NASDAQ": "纳斯达克", "AMEX": "美交所",
            "CBOE": "芝期所", "CME": "芝商所",
            "SSE": "上交所", "SZSE": "深交所",
            "SHFE": "上期所", "DCE": "大商所", "CZCE": "郑商所", "CFFEX": "中金所",
            "BINANCE": "币安", "OKEX": "OKX", "HUOBI": "火币",
        }
        return _map.get(self.value, self.value)


class Market(Enum):
    """Market identifier (TAF-specific).

    Simplified market grouping for routing and display.
    """
    HK = "HK"               # 港股
    US = "US"               # 美股
    CN = "CN"               # A股
    CRYPTO = "CRYPTO"       # 加密货币

    @property
    def cn(self) -> str:
        """Chinese display name."""
        _map = {"HK": "港股", "US": "美股", "CN": "A股", "CRYPTO": "加密货币"}
        return _map[self.value]

    @property
    def trading_days_per_year(self) -> int:
        """Annualized trading days for this market."""
        _map = {"HK": 245, "US": 252, "CN": 244, "CRYPTO": 365}
        return _map[self.value]

    @property
    def default_currency(self) -> str:
        """Default currency for this market."""
        _map = {"HK": "HKD", "US": "USD", "CN": "CNY", "CRYPTO": "USDT"}
        return _map[self.value]

    @property
    def lot_size(self) -> int:
        """Default lot size for this market."""
        _map = {"HK": 100, "US": 1, "CN": 100, "CRYPTO": 1}
        return _map[self.value]


class Interval(Enum):
    """K-line interval."""
    MINUTE = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    MINUTE_30 = "30m"
    HOUR = "1h"
    HOUR_4 = "4h"
    DAILY = "1d"
    WEEKLY = "1w"
    MONTHLY = "1M"

    @property
    def minutes(self) -> int:
        """Interval in minutes."""
        _map = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "4h": 240, "1d": 1440, "1w": 10080, "1M": 43200,
        }
        return _map[self.value]

    @property
    def bars_per_day(self, trading_hours: float = 6.5) -> float:
        """Expected bars per trading day (US default 6.5h)."""
        return (trading_hours * 60) / self.minutes


# ── Helper functions ──────────────────────────────────────────────────────────

def parse_market(symbol: str) -> Market:
    """Extract market from symbol.

    Examples:
        "HK.00700" -> Market.HK
        "US.AAPL" -> Market.US
        "AAPL" -> Market.US (default)
        "00700.HK" -> Market.HK
    """
    upper = symbol.upper()
    if upper.startswith("HK.") or upper.endswith(".HK"):
        return Market.HK
    elif upper.startswith("US.") or upper.endswith(".US"):
        return Market.US
    elif upper.startswith("CN.") or upper.endswith(".CN") or upper.endswith(".SH") or upper.endswith(".SZ"):
        return Market.CN
    else:
        return Market.US  # Default to US for ambiguous symbols


def parse_exchange(symbol: str) -> Exchange:
    """Extract exchange from symbol.

    Examples:
        "HK.00700" -> Exchange.SEHK
        "US.AAPL" -> Exchange.NASDAQ
        "00700.HK" -> Exchange.SEHK
    """
    market = parse_market(symbol)
    _map = {
        Market.HK: Exchange.SEHK,
        Market.US: Exchange.NASDAQ,
        Market.CN: Exchange.SSE,
        Market.CRYPTO: Exchange.BINANCE,
    }
    return _map[market]
