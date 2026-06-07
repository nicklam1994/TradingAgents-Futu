"""DataCollector: fetch all data once, serve windowed views to analyst agents."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import threading
import time
import pandas as pd
from stockstats import wrap
import io

from tradingagents.agents.utils.agent_utils import (
    get_stock_data,
    get_indicators,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_news,
    get_global_news,
    get_insider_transactions,
    get_sector_performance,
    get_trending_tickers,
)

INDICATORS = [
    "close_50_sma", "close_200_sma", "close_10_ema",
    "rsi", "macd", "boll", "boll_ub", "boll_lb", "atr", "vwma",
]
SHORT_DAYS = 14
LONG_DAYS = 90

import numpy as np

_OHLCV_COLS = ["date", "open", "high", "low", "close", "volume"]


def _parse_csv_to_dataframe(raw_csv: str) -> Optional[pd.DataFrame]:
    """Parse raw CSV string into a normalized OHLCV DataFrame.

    Returns None if parsing fails or the CSV is too short/empty.
    """
    if not isinstance(raw_csv, str) or len(raw_csv) <= 50:
        return None
    try:
        df = pd.read_csv(io.StringIO(raw_csv), on_bad_lines='skip', comment='#')
    except Exception:
        return None
    if df.empty:
        return None
    cols_map = {c.lower(): c for c in df.columns}
    rename_dict = {}
    for target in _OHLCV_COLS:
        if target in cols_map:
            rename_dict[cols_map[target]] = target
    df = df.rename(columns=rename_dict)
    return df


# ── VPA (Volume Price Analysis) 预计算 ──────────────────────────


def _compute_vpa_indicators(df: pd.DataFrame, window: int = 20) -> str:
    """Pre-compute Volume Price Analysis indicators from OHLCV DataFrame.

    V2 — Enhanced with:
    1. AMA (Adaptive Moving Average) for volume smoothing
    2. Price-Volume Resonance scoring
    3. Correlation-based divergence detection

    Returns a human-readable text block for the VPA analyst agent.
    All numerical comparisons are done here so the LLM only needs to
    interpret the results, not do arithmetic.
    """
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(df.columns)):
        return "VPA 数据不足：缺少 OHLCV 列"

    df = df.copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"])

    if len(df) < window + 10:
        return "VPA 数据不足：历史 K 线数量不够"

    # ═══════════════════════════════════════════════════════════════
    # 1. AMA (Adaptive Moving Average) for volume
    # ═══════════════════════════════════════════════════════════════
    # Kaufman AMA: adapts smoothing based on market volatility
    # ER = Direction / Volatility; SC = (ER*(Fast-Slow)+Slow)^2
    n = 10  # AMA lookback
    fast_sc = 2.0 / (2.0 + 1.0)
    slow_sc = 2.0 / (30.0 + 1.0)

    vol = df["volume"].values.astype(float)
    ama_vol = np.zeros(len(vol))
    ama_vol[:n] = np.nan
    ama_vol[n - 1] = np.mean(vol[:n])
    for i in range(n, len(vol)):
        direction = abs(vol[i] - vol[i - n])
        volatility = np.sum(np.abs(np.diff(vol[i - n:i + 1])))
        er = direction / volatility if volatility > 0 else 0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        ama_vol[i] = ama_vol[i - 1] + sc * (vol[i] - ama_vol[i - 1])

    df["vol_ama"] = ama_vol
    df["vol_ama_ratio"] = vol / ama_vol

    # Traditional MA for comparison
    df["vol_ma"] = df["volume"].rolling(window).mean()
    df["volume_ratio"] = df["volume"] / df["vol_ma"]

    # ═══════════════════════════════════════════════════════════════
    # 2. Basic bar metrics
    # ═══════════════════════════════════════════════════════════════
    hl_range = df["high"] - df["low"]
    df["bar_spread"] = hl_range / df["close"]
    df["close_position"] = np.where(hl_range > 0, (df["close"] - df["low"]) / hl_range, 0.5)
    df["bar_type"] = np.where(
        df["close"] > df["open"], "阳线",
        np.where(df["close"] < df["open"], "阴线", "十字星"),
    )
    df["upper_shadow"] = np.where(hl_range > 0, (df["high"] - np.maximum(df["open"], df["close"])) / hl_range, 0.0)
    df["lower_shadow"] = np.where(hl_range > 0, (np.minimum(df["open"], df["close"]) - df["low"]) / hl_range, 0.0)
    df["pct_change"] = df["close"].pct_change()

    # ═══════════════════════════════════════════════════════════════
    # 3. Price-Volume Resonance (价量共振)
    # ═══════════════════════════════════════════════════════════════
    df["vol_trend_ratio"] = df["volume"].rolling(5).mean() / df["vol_ma"]

    conditions = [
        (df["pct_change"] > 0) & (df["vol_ama_ratio"] > 1.2),   # 强多
        (df["pct_change"] > 0) & (df["vol_ama_ratio"] <= 1.2),   # 弱多
        (df["pct_change"] < 0) & (df["vol_ama_ratio"] < 0.8),    # 弱空(卖压衰)
        (df["pct_change"] < 0) & (df["vol_ama_ratio"] >= 0.8),   # 强空
    ]
    choices = ["强多(涨+放量)", "弱多(涨+缩量)", "弱空(跌+缩量)", "强空(跌+放量)"]
    df["resonance"] = np.select(conditions, choices, default="中性")
    df["vp_harmony"] = df["resonance"]  # backward compat

    # ═══════════════════════════════════════════════════════════════
    # 4. OBV trend
    # ═══════════════════════════════════════════════════════════════
    close_diff = df["close"].diff()
    obv_sign = np.where(close_diff > 0, 1, np.where(close_diff < 0, -1, 0))
    obv_sign[0] = 0
    df["obv"] = (obv_sign * df["volume"].values).cumsum()
    obv_ma = df["obv"].rolling(10).mean()
    obv_trend = "上升" if len(obv_ma.dropna()) >= 5 and obv_ma.iloc[-1] > obv_ma.iloc[-5] else "下降"

    # ═══════════════════════════════════════════════════════════════
    # 5. Correlation-based Divergence (5日滚动相关性)
    # ═══════════════════════════════════════════════════════════════
    corr_window = 5
    df["pv_correlation"] = df["close"].rolling(corr_window).corr(df["volume"])
    last_corr = df["pv_correlation"].iloc[-1] if pd.notna(df["pv_correlation"].iloc[-1]) else 0

    # ═══════════════════════════════════════════════════════════════
    # 6. Format output
    # ═══════════════════════════════════════════════════════════════
    output_days = min(30, len(df) - window)
    recent = df.tail(output_days).copy()
    last = recent.iloc[-1]

    lines = []
    lines.append(f"## VPA 预计算指标（{window}日均量基准 + AMA 自适应均线）\n")

    ama_val = last.get("vol_ama", 0)
    ma_val = last.get("vol_ma", 0)
    ama_ratio = last.get("vol_ama_ratio", 0)
    lines.append(f"**AMA 成交量均线**: {ama_val:,.0f} (vs 传统MA: {ma_val:,.0f})")
    lines.append(f"**当前量/AMA比**: {ama_ratio:.2f} — {'放量' if ama_ratio > 1.2 else ('缩量' if ama_ratio < 0.8 else '平稳')}")
    lines.append(f"**OBV 趋势(10日)**: {obv_trend}")

    vol_5d = recent["volume"].tail(5).mean()
    vol_20d = last.get("vol_ma", 0)
    vol_summary = "放量" if vol_5d > vol_20d * 1.2 else ("缩量" if vol_5d < vol_20d * 0.8 else "平稳")
    lines.append(f"**近5日量能趋势**: {vol_summary}（5日均量/20日均量 = {last.get('vol_trend_ratio', 0):.2f}）")

    if last_corr > 0.5:
        corr_label = "正相关(量价同步)"
    elif last_corr < -0.3:
        corr_label = "负相关(量价背离⚠)"
    else:
        corr_label = "弱相关(中性)"
    lines.append(f"**5日量价相关性**: {last_corr:.2f} — {corr_label}\n")

    # Daily data table
    lines.append("### 逐日量价数据\n")
    lines.append("| 日期 | 类型 | 涨跌幅 | 实体 | 收盘位 | 量比(MA) | 量比(AMA) | 共振信号 |")
    lines.append("|------|------|--------|------|--------|----------|-----------|----------|")

    for _, row in recent.iterrows():
        dt = row.get("date", "")
        if hasattr(dt, "strftime"):
            dt = dt.strftime("%m-%d")
        else:
            dt = str(dt)[-5:]
        pct = row["pct_change"] * 100 if pd.notna(row["pct_change"]) else 0
        spread_label = "宽" if row["bar_spread"] > 0.03 else ("窄" if row["bar_spread"] < 0.015 else "中")
        cp = row["close_position"]
        cp_label = "高位" if cp > 0.7 else ("低位" if cp < 0.3 else "中位")
        vr = row.get("volume_ratio", 0)
        vr_ama = row.get("vol_ama_ratio", 0)
        vr_str = f"{vr:.1f}" if pd.notna(vr) else "N/A"
        vr_ama_str = f"{vr_ama:.1f}" if pd.notna(vr_ama) else "N/A"
        resonance = row.get("resonance", "中性")
        lines.append(
            f"| {dt} | {row['bar_type']} | {pct:+.1f}% | {spread_label}({row['bar_spread']:.3f}) "
            f"| {cp_label}({cp:.2f}) | {vr_str} | {vr_ama_str} | {resonance} |"
        )

    # ═══════════════════════════════════════════════════════════════
    # 7. Pattern recognition
    # ═══════════════════════════════════════════════════════════════
    lines.append("\n### 关键量价模式识别\n")

    last5 = recent.tail(5)
    price_up = last5["close"].iloc[-1] > last5["close"].iloc[0]
    vol_down = last5["volume"].iloc[-1] < last5["volume"].iloc[0]
    price_down = last5["close"].iloc[-1] < last5["close"].iloc[0]
    vol_up = last5["volume"].iloc[-1] > last5["volume"].iloc[0]

    if price_up and vol_down:
        lines.append("- **⚠ 顶部背离信号**: 近5日价格上涨但成交量递减，上涨动能可能衰竭")
    if price_down and vol_up:
        lines.append("- **⚠ 底部放量信号**: 近5日价格下跌但成交量递增，可能是恐慌抛售或换手")
    if price_down and vol_down:
        lines.append("- **卖压衰竭信号**: 近5日价格下跌且成交量递减，空方力量可能枯竭")
    if price_up and vol_up:
        lines.append("- **健康上涨信号**: 近5日价格上涨且成交量配合递增")

    # AMA-based signals
    if ama_ratio > 1.5 and last["pct_change"] > 0:
        lines.append("- **AMA 放量突破**: 成交量显著突破自适应均线，趋势确认信号强")
    elif ama_ratio < 0.5:
        lines.append("- **AMA 极度缩量**: 成交量远低于自适应均线，市场观望或即将变盘")

    # Correlation divergence
    if last_corr < -0.5:
        lines.append(f"- **量价高度背离**: 5日相关性 {last_corr:.2f}，价格与成交量方向严重不一致，反转概率高")

    # Selling climax
    for i in range(-3, 0):
        if i < -len(recent):
            continue
        row = recent.iloc[i]
        if (row.get("vol_ama_ratio", 0) > 2.0
                and row.get("pct_change", 0) < -0.03
                and row.get("close_position", 0.5) > 0.5):
            lines.append(f"- **卖出高潮(Selling Climax)**: {str(row.get('date', ''))[-5:]} 急跌巨量但收盘收回过半，可能是恐慌见底")

    # 高位放量滞涨
    for i in range(-3, 0):
        if i < -len(recent):
            continue
        row = recent.iloc[i]
        if (row.get("vol_ama_ratio", 0) > 1.8
                and abs(row.get("pct_change", 0)) < 0.01
                and row.get("bar_spread", 0) < 0.015):
            lines.append(f"- **放量滞涨**: {str(row.get('date', ''))[-5:]} 巨量但价格几乎不动（窄实体），多空分歧大")

    if not any("**" in l for l in lines[-5:]):
        lines.append("- 近期无显著量价异常模式")

    return "\n".join(lines)


def make_cache_key(ticker: str, trade_date: str) -> str:
    return f"{ticker}_{trade_date}"


def _safe(tool, payload: dict) -> Any:
    start_t = time.time()
    try:
        res = tool.invoke(payload)
        duration = time.time() - start_t
        # 仅在耗时较长时输出
        if duration > 0.5:
            print(f"  [Timer] {getattr(tool, 'name', str(tool))} took {duration:.2f}s")
        return res
    except Exception as exc:
        return f"{getattr(tool, 'name', str(tool))} 调用失败：{type(exc).__name__}: {exc}"


def _fetch_all(ticker: str, trade_date: str) -> Dict[str, Any]:
    """Fetch all data sources in parallel.

    Always fetches full data including financial statements, regardless of horizon.
    The horizon only affects the analysis window, not data collection.
    """
    lookback = LONG_DAYS
    end_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    # 为了计算指标准确（如 200 SMA），需要比分析窗口更长的历史数据
    fetch_lookback = 365
    start_str = (end_dt - timedelta(days=fetch_lookback)).strftime("%Y-%m-%d")

    tasks: Dict[str, tuple] = {
        "stock_data": (get_stock_data, {"symbol": ticker, "start_date": start_str, "end_date": trade_date}),
        "news": (get_news, {"ticker": ticker, "start_date": (end_dt - timedelta(days=lookback)).strftime("%Y-%m-%d"), "end_date": trade_date}),
        "global_news": (get_global_news, {"curr_date": trade_date, "look_back_days": lookback, "limit": 30}),
        "sector_performance": (get_sector_performance, {"market": "US", "top_n": 15}),
        "trending_tickers": (get_trending_tickers, {"market": "US", "top_n": 20}),
        "insider_transactions": (get_insider_transactions, {"ticker": ticker}),
    }

    # 财务报表类数据始终拉取，Research Manager 根据 horizon 自行判断权重
    tasks.update({
        "fundamentals": (get_fundamentals, {"ticker": ticker, "curr_date": trade_date}),
        "balance_sheet": (get_balance_sheet, {"ticker": ticker, "freq": "quarterly", "curr_date": trade_date}),
        "cashflow": (get_cashflow, {"ticker": ticker, "freq": "quarterly", "curr_date": trade_date}),
        "income_statement": (get_income_statement, {"ticker": ticker, "freq": "quarterly", "curr_date": trade_date}),
    })

    results: Dict[str, Any] = {}
    fetch_start = time.time()
    # 减少并发池大小，避免被反爬
    with ThreadPoolExecutor(max_workers=min(10, len(tasks))) as executor:
        future_to_key = {executor.submit(_safe, tool, payload): key for key, (tool, payload) in tasks.items()}
        for future in future_to_key:
            results[future_to_key[future]] = future.result()

    # ── Parse CSV once, reuse for indicators and VPA ──────────────────
    raw_csv = results.get("stock_data", "")
    df = _parse_csv_to_dataframe(raw_csv)

    # ── 核心加速：本地计算所有技术指标 ──────────────────
    indicators_res = {}
    try:
        if df is not None and "close" in df.columns:
            ss = wrap(df.copy())

            calc_map = {
                "close_50_sma": "close_50_sma",
                "close_200_sma": "close_200_sma",
                "close_10_ema": "close_10_ema",
                "rsi": "rsi_14",
                "macd": "macd",
                "boll": "close_20_sma",
                "boll_ub": "boll_ub",
                "boll_lb": "boll_lb",
                "atr": "atr",
                "vwma": "vwma"
            }

            for key, ss_key in calc_map.items():
                try:
                    val = ss[ss_key].iloc[-1]
                    indicators_res[key] = round(float(val), 2) if isinstance(val, (int, float)) else str(val)
                except Exception:
                    indicators_res[key] = "N/A"
        else:
            print(f"  [Warning] No valid stock_data for indicator calculation.")
    except Exception as e:
        print(f"  [Error] Local indicator calculation failed: {e}")

    for ind in INDICATORS:
        if ind not in indicators_res:
            indicators_res[ind] = "无数据"

    results["indicators"] = indicators_res

    # ── VPA 预计算指标 ──────────────────────────────
    try:
        if df is not None:
            results["vpa_indicators"] = _compute_vpa_indicators(df.copy())
        else:
            results["vpa_indicators"] = "VPA 数据不足"
    except Exception as e:
        results["vpa_indicators"] = f"VPA 计算失败：{e}"

    print(f"[Timer] Total Data Collection for {ticker} took {time.time() - fetch_start:.2f}s")
    return results


class DataCollector:
    """Collect and cache data, thread-safe and shareable across jobs."""

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()
        self._refcounts: Dict[str, int] = {}

    def _get_key_lock(self, key: str) -> threading.Lock:
        with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def collect(self, ticker: str, trade_date: str, horizons: Optional[List[str]] = None) -> Dict[str, Any]:
        """Fetch all data and store in cache.

        Thread-safe: concurrent calls for the same ticker+date will block
        on a per-key lock, so data is fetched only once.
        """
        key = make_cache_key(ticker, trade_date)
        key_lock = self._get_key_lock(key)
        with key_lock:
            if key not in self._cache:
                self._cache[key] = _fetch_all(ticker, trade_date)
        return self._cache[key]

    def get(self, ticker: str, trade_date: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached pool, or None if not collected yet."""
        return self._cache.get(make_cache_key(ticker, trade_date))

    def get_window(
        self,
        pool: Dict[str, Any],
        horizon: str,
        trade_date: str,
    ) -> Dict[str, Any]:
        """Return pool copy annotated with horizon window metadata."""
        days = SHORT_DAYS if horizon == "short" else LONG_DAYS
        result = dict(pool)
        result["_data_window"] = f"{days}天"
        result["_horizon"] = horizon
        return result

    def ref(self, ticker: str, trade_date: str) -> None:
        """Increment reference count (call before using cached data)."""
        key = make_cache_key(ticker, trade_date)
        with self._meta_lock:
            self._refcounts[key] = self._refcounts.get(key, 0) + 1

    def evict(self, ticker: str, trade_date: str) -> None:
        """Decrement refcount and remove cached data when no one needs it."""
        key = make_cache_key(ticker, trade_date)
        with self._meta_lock:
            count = self._refcounts.get(key, 1) - 1
            if count <= 0:
                self._cache.pop(key, None)
                self._refcounts.pop(key, None)
                # 不删除 _locks[key]：其他线程可能仍持有该锁的引用，
                # 删除会导致新 collect() 创建新锁，破坏互斥。
                # 锁对象很轻量，留着不影响内存。
            else:
                self._refcounts[key] = count
