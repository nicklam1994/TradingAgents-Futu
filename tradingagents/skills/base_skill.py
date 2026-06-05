"""Strategy Plugin Base — Abstract base class for all trading strategy plugins.

Every strategy plugin must subclass ``BaseSkill`` and implement:
    - ``analyze(market_data) -> Signal``: core analysis logic
    - ``name`` (property): unique strategy identifier
    - ``version`` (property): semver string
    - ``description`` (property): human-readable summary

The plugin system uses this ABC for:
    - SkillRegistry: discovers and registers all BaseSkill subclasses
    - SkillRouter: selects appropriate skills for current market state
    - SkillAggregator: combines multiple skill signals into consensus

Usage:
    class MySkill(BaseSkill):
        @property
        def name(self) -> str:
            return "my_skill"

        @property
        def version(self) -> str:
            return "1.0.0"

        @property
        def description(self) -> str:
            return "My custom strategy"

        def analyze(self, market_data: MarketData) -> Signal:
            return Signal(direction="buy", confidence=0.8, reason="trend up")
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Signal types ─────────────────────────────────────────────────────────────

class SignalDirection(str, Enum):
    """Direction of a trading signal."""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class MarketRegime(str, Enum):
    """Broad market state classification."""
    BULL = "bull"
    BEAR = "bear"
    RANGEBOUND = "rangebound"  # 震盪
    UNKNOWN = "unknown"


@dataclass
class MarketData:
    """Normalized input for strategy analysis.

    Attributes:
        symbol:        Stock ticker (e.g., "HK.00700").
        price:         Current market price.
        prices:        Historical close prices (most recent last).
        volumes:       Historical volumes aligned with prices.
        high_52w:      52-week high.
        low_52w:       52-week low.
        pe_ratio:      Price-to-earnings ratio (None if unavailable).
        pb_ratio:      Price-to-book ratio.
        dividend_yield: Dividend yield as fraction (e.g., 0.03 = 3%).
        market_regime: Current market regime (bull/bear/rangebound).
        sector:        Sector classification.
        metadata:      Arbitrary extra data from data providers.
    """
    symbol: str = ""
    price: float = 0.0
    prices: List[float] = field(default_factory=list)
    volumes: List[float] = field(default_factory=list)
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    dividend_yield: Optional[float] = None
    market_regime: MarketRegime = MarketRegime.UNKNOWN
    sector: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Signal:
    """Output of a strategy's ``analyze()`` method.

    Attributes:
        direction:  BUY / SELL / HOLD.
        confidence: 0.0–1.0 confidence in the signal.
        reason:     Human-readable explanation.
        target_price: Optional price target.
        stop_loss:  Optional stop-loss price.
        weight:     Strategy's self-assigned weight (used by SkillAggregator).
        metadata:   Strategy-specific extra data (indicators, scores, etc.).
    """
    direction: SignalDirection
    confidence: float = 0.5
    reason: str = ""
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    weight: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for API/logging."""
        result: Dict[str, Any] = {
            "direction": self.direction.value,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "weight": self.weight,
        }
        if self.target_price is not None:
            result["target_price"] = self.target_price
        if self.stop_loss is not None:
            result["stop_loss"] = self.stop_loss
        if self.metadata:
            result["metadata"] = self.metadata
        return result


# ── Base Skill ABC ───────────────────────────────────────────────────────────

class BaseSkill(ABC):
    """Abstract base for all trading strategy plugins.

    Subclasses MUST implement:
        - ``analyze(market_data) -> Signal``
        - ``name`` (property)
        - ``version`` (property)
        - ``description`` (property)

    Optional overrides:
        - ``supported_regimes``: list of MarketRegime this skill excels in.
        - ``setup()``: called once after registration (e.g., load models).
        - ``teardown()``: called before unregistration.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier (e.g., 'momentum', 'value')."""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """Semantic version string (e.g., '1.0.0')."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line human-readable summary."""
        ...

    @property
    def supported_regimes(self) -> List[MarketRegime]:
        """Market regimes where this skill performs best.

        Override to declare preferred conditions.  The SkillRouter uses
        this to prefer skills that match the current market state.

        Default: all regimes (skill is always eligible).
        """
        return list(MarketRegime)

    @abstractmethod
    def analyze(self, market_data: MarketData) -> Signal:
        """Run the strategy analysis and return a signal.

        Args:
            market_data: Normalized market data for the target symbol.

        Returns:
            Signal with direction, confidence, and reasoning.
        """
        ...

    def setup(self) -> None:
        """One-time initialization after registration.

        Override to load models, fetch configuration, etc.
        Called by SkillRegistry after successful registration.
        """
        pass

    def teardown(self) -> None:
        """Cleanup before unregistration.

        Override to release resources, close connections, etc.
        """
        pass

    def to_info(self) -> Dict[str, Any]:
        """Return skill metadata as a dict."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "supported_regimes": [r.value for r in self.supported_regimes],
            "class": self.__class__.__name__,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} v{self.version}>"
