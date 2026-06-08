"""YAML Strategy Loader — Load DSA-style YAML strategy definitions.

Loads YAML strategy files from the strategies/ directory and provides
them as prompt instructions for the trader agent.

Usage:
    from tradingagents.strategies.yaml_loader import load_strategy, list_strategies

    strategies = list_strategies()
    strategy = load_strategy("bull_trend")
    instructions = strategy["instructions"]
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Directory containing YAML strategy files
_STRATEGIES_DIR = Path(__file__).parent


def list_strategies() -> List[Dict[str, Any]]:
    """List all available YAML strategies.

    Returns:
        List of strategy metadata dicts with keys:
        - name: Strategy identifier
        - display_name: Human-readable name
        - description: Strategy description
        - category: Strategy category
        - market_regimes: List of suitable market regimes
        - default_priority: Priority value (lower = higher priority)
    """
    strategies = []

    for yaml_file in sorted(_STRATEGIES_DIR.glob("*.yaml")):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if not data or not isinstance(data, dict):
                continue

            strategies.append({
                "name": data.get("name", yaml_file.stem),
                "display_name": data.get("display_name", yaml_file.stem),
                "description": data.get("description", ""),
                "category": data.get("category", "general"),
                "market_regimes": data.get("market_regimes", []),
                "default_priority": data.get("default_priority", 100),
                "default_active": data.get("default_active", False),
                "default_router": data.get("default_router", False),
                "aliases": data.get("aliases", []),
            })
        except Exception as e:
            logger.warning("Failed to load strategy %s: %s", yaml_file, e)

    return strategies


def load_strategy(name: str) -> Optional[Dict[str, Any]]:
    """Load a strategy by name or alias.

    Args:
        name: Strategy name or alias (e.g., "bull_trend" or "趋势")

    Returns:
        Strategy dict with all fields, or None if not found.
    """
    # Try direct file lookup first
    yaml_file = _STRATEGIES_DIR / f"{name}.yaml"
    if yaml_file.exists():
        return _load_yaml_file(yaml_file)

    # Search by name or alias
    for yaml_file in _STRATEGIES_DIR.glob("*.yaml"):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if not data or not isinstance(data, dict):
                continue

            # Check name
            if data.get("name") == name:
                return data

            # Check aliases
            aliases = data.get("aliases", [])
            if name in aliases:
                return data

        except Exception as e:
            logger.warning("Failed to load strategy %s: %s", yaml_file, e)

    return None


def _load_yaml_file(yaml_file: Path) -> Optional[Dict[str, Any]]:
    """Load a single YAML strategy file."""
    try:
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("Failed to load strategy %s: %s", yaml_file, e)
        return None


def get_strategy_instructions(name: str) -> str:
    """Get strategy instructions as a prompt string.

    Args:
        name: Strategy name or alias.

    Returns:
        Strategy instructions text, or empty string if not found.
    """
    strategy = load_strategy(name)
    if not strategy:
        return ""

    instructions = strategy.get("instructions", "")
    if not instructions:
        return ""

    # Format as a clear strategy section
    display_name = strategy.get("display_name", name)
    description = strategy.get("description", "")

    return f"""
## 当前应用策略：{display_name}

{description}

{instructions}
"""


def get_default_strategy() -> Optional[str]:
    """Get the default active strategy name.

    Returns:
        Name of the default active strategy, or None.
    """
    for yaml_file in sorted(_STRATEGIES_DIR.glob("*.yaml")):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if data and isinstance(data, dict) and data.get("default_active"):
                return data.get("name", yaml_file.stem)
        except Exception:
            continue

    return None
