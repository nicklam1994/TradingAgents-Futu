"""
Type-safe data objects for TradingAgents-Futu.

Replaces dict-based data passing with typed dataclasses.
Extracted from api/services/sim_trading_service.py + vnpy patterns.

Phase 13.1: Dataclass data objects
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Account & Position ───────────────────────────────────────────────────────

@dataclass
class AccountInfo:
    """Account financial information."""
    market: str = "HK"              # 市场 (HK/US)
    total_assets: float = 0.0       # 总资产
    cash_balance: float = 0.0       # 现金余额
    frozen_cash: float = 0.0        # 冻结资金
    market_val: float = 0.0         # 持仓市值
    currency: str = "HKD"
    available_cash: float = 0.0     # 可用资金（买入力）
    unrealized_pnl: float = 0.0     # 未实现盈亏
    realized_pnl: float = 0.0       # 已实现盈亏


@dataclass
class Position:
    """Single position entry."""
    code: str = ""                  # 股票代码 (e.g., "HK.00700", "US.AAPL")
    symbol: str = ""                # 原始符号
    stock_name: str = ""            # 股票名称
    qty: int = 0                    # 持仓数量
    cost_price: float = 0.0         # 成本价
    current_price: float = 0.0      # 现价
    prev_close: float = 0.0         # 昨收价
    market_val: float = 0.0         # 市值
    cost_val: float = 0.0           # 成本市值
    unrealized_pnl: float = 0.0     # 持仓盈亏
    unrealized_pnl_pct: float = 0.0 # 持仓盈亏%
    today_pnl: float = 0.0          # 今日盈亏
    currency: str = "HKD"


# ── Order & Trade ────────────────────────────────────────────────────────────

@dataclass
class OrderResult:
    """Result of placing an order."""
    order_id: str = ""
    code: str = ""
    side: str = ""
    price: float = 0.0
    qty: float = 0.0
    status: str = ""
    create_time: str = ""


@dataclass
class OrderInfo:
    """Order detail."""
    order_id: str = ""
    code: str = ""
    stock_name: str = ""
    side: str = ""
    order_type: str = ""
    price: float = 0.0
    qty: float = 0.0
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    status: str = ""
    create_time: str = ""
    updated_time: str = ""
    currency: str = "HKD"


@dataclass
class DealInfo:
    """Executed deal detail."""
    deal_id: str = ""
    code: str = ""
    stock_name: str = ""
    side: str = ""
    deal_market: str = ""
    order_type: str = ""
    qty: float = 0.0
    price: float = 0.0
    create_time: str = ""
    status: str = ""
    currency: str = "HKD"


# ── Market Data (new) ────────────────────────────────────────────────────────

@dataclass
class BarData:
    """K-line bar data (OHLCV).

    Replaces CSV string passing between FutuProvider and DataCollector.
    Compatible with vnpy/trader/object.py BarData pattern.
    """
    symbol: str = ""                # 股票代码 (e.g., "HK.00700", "US.AAPL")
    datetime: Optional[datetime] = None  # K线时间
    interval: str = "1d"            # 周期 (1m/5m/15m/1h/4h/1d)
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0
    volume: float = 0.0             # 成交量
    turnover: float = 0.0           # 成交额

    def to_csv_row(self) -> str:
        """Convert to CSV row string for backward compatibility."""
        dt = self.datetime.strftime("%Y-%m-%d") if self.datetime else ""
        return f"{dt},{self.open_price},{self.high_price},{self.low_price},{self.close_price},{self.volume}"

    @staticmethod
    def csv_header() -> str:
        """CSV header for backward compatibility."""
        return "Date,Open,High,Low,Close,Volume"


@dataclass
class TickData:
    """Real-time tick data.

    Used by WebSocket quote manager (quote_ws_manager.py).
    Compatible with vnpy/trader/object.py TickData pattern.
    """
    symbol: str = ""                # 股票代码
    datetime: Optional[datetime] = None  # Tick时间
    last_price: float = 0.0         # 最新价
    last_volume: float = 0.0        # 最新成交量
    bid_price: float = 0.0          # 买一价
    bid_volume: float = 0.0         # 买一量
    ask_price: float = 0.0          # 卖一价
    ask_volume: float = 0.0         # 卖一量
    high_price: float = 0.0         # 最高价
    low_price: float = 0.0          # 最低价
    open_price: float = 0.0         # 开盘价
    prev_close: float = 0.0         # 昨收价
    volume: float = 0.0             # 总成交量
    turnover: float = 0.0           # 总成交额
    change: float = 0.0             # 涨跌额
    change_pct: float = 0.0         # 涨跌幅%
    amplitude: float = 0.0          # 振幅%


@dataclass
class SignalData:
    """Analysis signal from agents (TAF-specific).

    Carries the output from Market Analyst, Social Analyst, etc.
    Used by autonomous_loop.py and stock_selector.py.
    """
    symbol: str = ""                # 股票代码
    direction: str = ""             # "buy" / "sell" / "hold"
    confidence: float = 0.0         # 置信度 (0-1)
    target_price: float = 0.0       # 目标价
    stop_loss: float = 0.0          # 止损价
    source: str = ""                # 来源 (e.g., "market_analyst", "social_analyst")
    risk_flags: list[str] = field(default_factory=list)  # 风险标记
    created_at: Optional[datetime] = None
