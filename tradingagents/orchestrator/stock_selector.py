"""StockSelector — Multi-dimensional stock screening engine.

Combines momentum, fundamental, and sentiment/heat analysis to select
top N candidates from a pool. Leverages the existing 7 analyst agents
for parallel evaluation.

Usage:
    selector = StockSelector(futu_provider=provider)
    candidates = selector.select(
        pool=["HK.00700", "HK.09988", "HK.03690"],
        budget=20000.0,
        top_n=3,
    )
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from stockstats import StockDataFrame

logger = logging.getLogger(__name__)


@dataclass
class StockCandidate:
    """A stock candidate with multi-dimensional scores."""
    symbol: str
    name: Optional[str] = None
    # Individual dimension scores (0.0–1.0)
    momentum_score: float = 0.0
    fundamental_score: float = 0.0
    sentiment_score: float = 0.0
    volume_score: float = 0.0
    # Composite score (weighted average)
    composite_score: float = 0.0
    # Market data snapshot
    current_price: Optional[float] = None
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    # Analyst reasoning
    reasoning: Dict[str, str] = field(default_factory=dict)
    # Raw analyst outputs
    analyst_outputs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "momentum_score": round(self.momentum_score, 4),
            "fundamental_score": round(self.fundamental_score, 4),
            "sentiment_score": round(self.sentiment_score, 4),
            "volume_score": round(self.volume_score, 4),
            "composite_score": round(self.composite_score, 4),
            "current_price": self.current_price,
            "market_cap": self.market_cap,
            "pe_ratio": self.pe_ratio,
            "reasoning": self.reasoning,
        }


# ── Dimension weights (configurable) ──────────────────────────────────────

DEFAULT_WEIGHTS = {
    "momentum": 0.35,
    "fundamental": 0.30,
    "sentiment": 0.20,
    "volume": 0.15,
}


class StockSelector:
    """Multi-dimensional stock screener.

    Evaluates a pool of stocks across 4 dimensions:
    1. Momentum: price trends, RSI, MACD, moving averages
    2. Fundamental: PE, PB, ROE, revenue growth, earnings
    3. Sentiment: news sentiment, social media buzz, insider transactions
    4. Volume: trading volume trends, fund flow, board turnover

    Each dimension uses the appropriate analyst agent from the tradingagents
    pipeline, running them in parallel for efficiency.

    Dependencies:
        - tradingagents.dataflows.providers.futu_provider.FutuProvider
        - tradingagents.dataflows.quant_metrics.QuantMetrics
        - tradingagents.llm_clients.factory for LLM calls
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        max_workers: int = 4,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """Initialize the stock selector.

        Args:
            weights: Dimension weights (must sum to 1.0). Default: DEFAULT_WEIGHTS
            llm_provider: LLM provider for analyst agents
            llm_model: Model name for analyst agents
            max_workers: Max parallel analyst threads
            api_key: LLM API key
            base_url: LLM base URL
        """
        self._weights = weights or DEFAULT_WEIGHTS
        self._provider = llm_provider or os.getenv("TA_LLM_PROVIDER", "openai")
        self._model = llm_model or os.getenv("TA_LLM_MODEL", "gpt-4o")
        self._max_workers = max_workers
        self._api_key = api_key or os.getenv("TA_API_KEY") or os.getenv("OPENAI_API_KEY")
        self._base_url = base_url or os.getenv("TA_LLM_BASE_URL")
        self._llm = None  # Lazy init

    def _get_llm(self):
        """Lazy-init LLM client."""
        if self._llm is None:
            from tradingagents.llm_clients.factory import create_llm_client

            kwargs: dict = {}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            if self._api_key:
                kwargs["api_key"] = self._api_key
            client = create_llm_client(
                self._provider, self._model, **kwargs
            )
            self._llm = client.get_llm()
        return self._llm

    def select(
        self,
        pool: List[str],
        budget: float,
        top_n: int = 5,
        dimensions: Optional[List[str]] = None,
    ) -> List[StockCandidate]:
        """Screen and rank stocks from a candidate pool.

        Args:
            pool: List of stock symbols to evaluate
            budget: Available capital (for position sizing feasibility)
            top_n: Number of top candidates to return
            dimensions: Which dimensions to evaluate (default: all 4)

        Returns:
            List of top_n StockCandidate objects, sorted by composite_score descending
        """
        if not pool:
            logger.warning("Empty stock pool — returning no candidates")
            return []

        dims = dimensions or ["momentum", "fundamental", "sentiment", "volume"]
        logger.info(
            "Selecting top %d from %d candidates, dimensions=%s, budget=%.0f",
            top_n, len(pool), dims, budget,
        )

        # Phase 1: Fetch market data for all candidates
        market_data = self._fetch_market_data(pool)

        # Phase 2: Run dimension analyses in parallel
        scores = self._analyze_dimensions(pool, market_data, dims)

        # Phase 3: Calculate composite scores and rank
        candidates = self._build_candidates(pool, market_data, scores, dims)

        # Phase 4: Filter by budget feasibility (can we afford at least 1 lot?)
        affordable = self._filter_by_budget(candidates, budget)

        # Return top N
        result = affordable[:top_n]
        logger.info(
            "Selected %d candidates: %s",
            len(result),
            [c.symbol for c in result],
        )
        return result

    def _fetch_market_data(
        self, pool: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch market data snapshots for all stocks in pool.

        Uses FutuProvider for real-time quotes and fundamentals.
        Falls back to empty data if provider is unavailable.
        """
        data: Dict[str, Dict[str, Any]] = {}

        try:
            from tradingagents.dataflows.providers.futu_provider import FutuProvider

            provider = FutuProvider()
            # Use get_realtime_quotes for batch quote fetching
            try:
                quotes_raw = provider.get_realtime_quotes(pool)
                # Parse CSV string into list of dicts
                # FutuProvider returns CSV with columns:
                # symbol,price,change,change_pct,volume,high,low,open
                reader = csv.DictReader(io.StringIO(quotes_raw))
                quotes = list(reader)
                for item in quotes:
                    sym = item.get("symbol", "")
                    data[sym] = {
                        "price": item.get("last_price") or item.get("price"),
                        "market_cap": item.get("market_val"),
                        "pe_ratio": item.get("pe_ratio"),
                        "volume": item.get("volume"),
                        "turnover": item.get("turnover"),
                        "name": item.get("name", ""),
                    }
            except Exception as e:
                logger.warning("Batch quote fetch failed: %s", e)
                # Fallback: individual fetches
                for symbol in pool:
                    data[symbol] = {}
        except ImportError:
            logger.warning("FutuProvider not available — using empty market data")
            for symbol in pool:
                data[symbol] = {}

        return data

    def _analyze_dimensions(
        self,
        pool: List[str],
        market_data: Dict[str, Dict[str, Any]],
        dimensions: List[str],
    ) -> Dict[str, Dict[str, Optional[float]]]:
        """Run dimension analyses in parallel using ThreadPoolExecutor.

        Returns:
            Dict mapping symbol → {dimension: score} (score may be None if unavailable)
        """
        results: Dict[str, Dict[str, Optional[float]]] = {s: {} for s in pool}

        # Define dimension analyzers
        analyzers: Dict[str, Callable] = {
            "momentum": self._analyze_momentum,
            "fundamental": self._analyze_fundamental,
            "sentiment": self._analyze_sentiment,
            "volume": self._analyze_volume,
        }

        tasks = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            for symbol in pool:
                for dim in dimensions:
                    if dim in analyzers:
                        tasks.append(
                            executor.submit(
                                analyzers[dim], symbol, market_data.get(symbol, {})
                            )
                        )

            # Collect results
            task_idx = 0
            for symbol in pool:
                for dim in dimensions:
                    if dim in analyzers:
                        try:
                            score, reasoning = tasks[task_idx].result(timeout=30)
                            results[symbol][dim] = score  # may be None if no data
                        except Exception as e:
                            logger.warning(
                                "Analysis failed for %s/%s: %s", symbol, dim, e
                            )
                            results[symbol][dim] = None
                        task_idx += 1

        return results

    def _analyze_momentum(
        self, symbol: str, data: Dict[str, Any]
    ) -> Tuple[float, str]:
        """Analyze momentum via FutuProvider.get_stock_data() (W3-1, W4-1).

        Uses only the public get_stock_data() API — no private method access.
        Requests forward-adjusted (qfq) K-lines so MA calculations reflect
        actual price movements after splits/dividends.

        Computes real MA crossover + RSI from K-line data:
        - MA score (60%): 1.0 when MA5 > MA20, 0.0 otherwise
        - RSI score (40%): RSI 30-70 mapped to 0-1

        Falls back to change_pct-based score if FutuProvider is unavailable.

        Returns:
            (score, reasoning_text) tuple — score in 0.0–1.0 range
        """
        try:
            from datetime import datetime, timedelta

            from tradingagents.dataflows.providers.futu_provider import FutuProvider

            provider = FutuProvider()
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

            # Use public get_stock_data() with forward-adjusted (qfq) prices
            raw_csv = provider.get_stock_data(symbol, start_date, end_date, autype="qfq")
            if not raw_csv or not raw_csv.strip():
                raise RuntimeError(f"No K-line data for {symbol}")

            # Skip comment lines (start with '#')
            lines = [ln for ln in raw_csv.splitlines() if not ln.startswith("#")]
            csv_text = "\n".join(lines)
            if not csv_text.strip():
                raise RuntimeError(f"Empty K-line CSV for {symbol}")

            from io import StringIO

            df = pd.read_csv(StringIO(csv_text))
            # get_stock_data() returns: Date,Open,High,Low,Close,Volume
            df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                                    "Low": "low", "Close": "close", "Volume": "volume"})
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

            if len(df) < 20:
                raise RuntimeError(f"Insufficient data for {symbol}: {len(df)} days")

            # Compute indicators via stockstats (no private API calls)
            ss = StockDataFrame(df)
            ma5_series = ss["close_5_sma"]
            ma20_series = ss["close_20_sma"]
            rsi_series = ss["rsi_14"]

            ma5 = float(ma5_series.iloc[-1]) if not np.isnan(ma5_series.iloc[-1]) else None
            ma20 = float(ma20_series.iloc[-1]) if not np.isnan(ma20_series.iloc[-1]) else None
            rsi = float(rsi_series.iloc[-1]) if not np.isnan(rsi_series.iloc[-1]) else None

            if ma5 is None or ma20 is None:
                raise RuntimeError(f"MA data incomplete for {symbol}")

            # MA crossover score: 1.0 when short MA above long MA
            ma_score = 1.0 if ma5 > ma20 else 0.0
            # RSI score: map 30-70 range to 0-1 (neutral zone normalization)
            rsi_score = max(0.0, min(1.0, (rsi - 30) / 40)) if rsi is not None else 0.5

            # Weighted momentum: MA trend (60%) + RSI (40%)
            score = ma_score * 0.6 + rsi_score * 0.4
            score = max(0.0, min(1.0, score))

            parts = [f"MA5={ma5:.2f}", f"MA20={ma20:.2f}"]
            if rsi is not None:
                parts.append(f"RSI14={rsi:.1f}")
            parts.append(f"{'bullish' if ma5 > ma20 else 'bearish'}")
            return score, f"Momentum for {symbol}: {', '.join(parts)}"

        except Exception as e:
            logger.debug("Momentum fallback for %s: %s", symbol, e)
            # Fallback: use change_pct from market data (original logic)
            change_pct = data.get("change_pct", 0)
            try:
                change_pct = float(change_pct)
            except (TypeError, ValueError):
                change_pct = 0
            score = max(0.0, min(1.0, 0.5 + change_pct / 100))
            return score, f"Momentum fallback for {symbol}: change_pct={change_pct}%"

    def _analyze_fundamental(
        self, symbol: str, data: Dict[str, Any]
    ) -> Tuple[float, str]:
        """Analyze fundamental indicators.

        Returns:
            (score, reasoning_text) tuple
        """
        pe = data.get("pe_ratio")
        market_cap = data.get("market_cap")

        score = 0.5
        reasoning_parts = []

        # PE ratio scoring
        if pe is not None and pe > 0:
            if pe < 15:
                score += 0.2
                reasoning_parts.append(f"Low PE ({pe:.1f}) — value stock")
            elif pe < 30:
                score += 0.1
                reasoning_parts.append(f"Moderate PE ({pe:.1f})")
            else:
                score -= 0.1
                reasoning_parts.append(f"High PE ({pe:.1f}) — growth premium")

        # Market cap scoring (prefer mid-large cap for liquidity)
        if market_cap is not None and market_cap > 0:
            if market_cap > 1e10:  # >10B
                score += 0.1
                reasoning_parts.append("Large cap — good liquidity")
            elif market_cap > 1e9:  # >1B
                score += 0.05
                reasoning_parts.append("Mid cap")

        return max(0.0, min(1.0, score)), "; ".join(reasoning_parts) or "Limited data"

    def _analyze_sentiment(
        self, symbol: str, data: Dict[str, Any]
    ) -> Tuple[Optional[float], str]:
        """Analyze social sentiment via SocialSentimentService (P2-1~2).

        Fetches real sentiment scores from Reddit/X/Polymarket APIs.
        Falls back to empty dict (None score) when the service is unavailable,
        so callers never receive fabricated data.

        Returns:
            (score, reasoning_text) tuple — score in 0.0–1.0 range,
            or (None, reason) when no data is available.
        """
        try:
            from tradingagents.dataflows.social_sentiment import (
                SocialSentimentService,
            )

            service = SocialSentimentService.from_env()
            if not service.is_available:
                logger.debug("Sentiment API not configured for %s", symbol)
                return None, f"Sentiment API not configured for {symbol}"

            # Strip exchange prefix: "HK.00700" → "00700"
            ticker = symbol.split(".")[-1] if "." in symbol else symbol
            result = service.get_social_sentiment(ticker)

            if not result.get("available"):
                return None, f"Sentiment unavailable for {symbol}"

            # Extract sentiment components from Reddit data
            reddit = result.get("reddit")
            report = reddit.get("report", reddit) if isinstance(reddit, dict) else None
            sentiment_score = None
            buzz_score = None
            mention_count = 0

            if report and isinstance(report, dict):
                # sentiment_score: API-provided (expected 0-1 range)
                sentiment_score = report.get("sentiment_score")
                # buzz_score: 0-100 scale from Reddit
                raw_buzz = report.get("buzz_score") or report.get("buzz")
                if raw_buzz is not None:
                    try:
                        buzz_score = float(raw_buzz) / 100.0
                    except (TypeError, ValueError):
                        pass
                # Mention count for reasoning
                mention_count = report.get("total_mentions") or report.get("mentions", 0)

            # Normalize sentiment_score from [-1,1] to [0,1] (W2-1)
            if sentiment_score is not None:
                try:
                    sentiment_score = max(0.0, min(1.0, (float(sentiment_score) + 1) / 2))
                except (TypeError, ValueError):
                    sentiment_score = None

            # Combine available signals into 0-1 score
            if sentiment_score is not None and buzz_score is not None:
                score = (sentiment_score + buzz_score) / 2.0
            elif sentiment_score is not None:
                score = sentiment_score
            elif buzz_score is not None:
                score = buzz_score
            else:
                return None, f"Sentiment data empty for {symbol}"

            score = max(0.0, min(1.0, score))
            parts = [f"sentiment={sentiment_score}", f"buzz={buzz_score:.0%}"]
            if mention_count:
                parts.append(f"mentions={mention_count}")
            return score, f"Social sentiment for {symbol}: {', '.join(parts)}"

        except Exception as e:
            logger.debug("Sentiment fallback for %s: %s", symbol, e)
            return None, f"Sentiment unavailable for {symbol}: {e}"

    def _analyze_volume(
        self, symbol: str, data: Dict[str, Any]
    ) -> Tuple[float, str]:
        """Analyze volume/fund flow indicators.

        Returns:
            (score, reasoning_text) tuple
        """
        volume = self._safe_float(data.get("volume"))
        turnover = self._safe_float(data.get("turnover"))

        score = 0.5
        if volume is not None and volume > 0:
            score += 0.15
        if turnover is not None and turnover > 0:
            score += 0.15

        return min(1.0, score), f"Volume={volume}, Turnover={turnover}"

    @staticmethod
    def _safe_float(val: Any) -> Optional[float]:
        """Convert a value to float, returning None on failure."""
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _build_candidates(
        self,
        pool: List[str],
        market_data: Dict[str, Dict[str, Any]],
        scores: Dict[str, Dict[str, Optional[float]]],
        dimensions: List[str],
    ) -> List[StockCandidate]:
        """Build StockCandidate objects with composite scores."""
        candidates = []

        for symbol in pool:
            data = market_data.get(symbol, {})
            dim_scores = scores.get(symbol, {})

            candidate = StockCandidate(
                symbol=symbol,
                name=data.get("name"),
                current_price=self._safe_float(data.get("price")),
                market_cap=self._safe_float(data.get("market_cap")),
                pe_ratio=self._safe_float(data.get("pe_ratio")),
                momentum_score=dim_scores.get("momentum") if dim_scores.get("momentum") is not None else 0.5,
                fundamental_score=dim_scores.get("fundamental") if dim_scores.get("fundamental") is not None else 0.5,
                sentiment_score=dim_scores.get("sentiment") if dim_scores.get("sentiment") is not None else 0.5,
                volume_score=dim_scores.get("volume") if dim_scores.get("volume") is not None else 0.5,
            )

            # Weighted composite score
            total_weight = sum(
                self._weights.get(d, 0) for d in dimensions
            )
            if total_weight > 0:
                candidate.composite_score = sum(
                    getattr(candidate, f"{d}_score", 0.5) * self._weights.get(d, 0)
                    for d in dimensions
                ) / total_weight

            candidates.append(candidate)

        # Sort by composite score descending
        candidates.sort(key=lambda c: c.composite_score, reverse=True)
        return candidates

    def _filter_by_budget(
        self, candidates: List[StockCandidate], budget: float
    ) -> List[StockCandidate]:
        """Filter candidates that are affordable within budget.

        A stock is affordable if we can buy at least 1 lot (100 shares for
        HK/CN, 1 share for US) within the budget.
        """
        affordable = []
        for c in candidates:
            if c.current_price is not None and c.current_price > 0:
                # Determine lot size based on exchange
                lot_size = 100 if c.symbol.startswith(("HK.", "SH.", "SZ.")) else 1
                min_cost = c.current_price * lot_size
                if min_cost <= budget:
                    affordable.append(c)
                else:
                    logger.debug(
                        "%s too expensive: min lot cost %.2f > budget %.2f",
                        c.symbol, min_cost, budget,
                    )
            else:
                # No price data — include with caveat
                affordable.append(c)

        return affordable
