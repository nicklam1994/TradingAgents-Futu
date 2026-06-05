"""Skill Registry — Registration and discovery of strategy plugins.

Provides a central registry where BaseSkill subclasses are registered,
queried, and auto-discovered from the ``builtin/`` package.

Usage:
    registry = SkillRegistry()
    registry.discover()                       # auto-scan builtin/
    registry.register(MyCustomSkill())        # manual registration
    skill = registry.get("momentum")
    all_skills = registry.list_all()
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import pkgutil
from typing import Dict, List, Optional, Type

from tradingagents.skills.base_skill import BaseSkill

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Central registry for trading strategy plugins.

    Features:
        - Register/unregister skills by name.
        - Auto-discover skills from the ``tradingagents.skills.builtin`` package.
        - Lookup by name, list all, or filter by regime.
    """

    def __init__(self):
        self._skills: Dict[str, BaseSkill] = {}
        self._discovered: bool = False

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, skill: BaseSkill) -> None:
        """Register a skill instance.

        Args:
            skill: An instantiated BaseSkill subclass.

        Raises:
            TypeError: If skill is not a BaseSkill subclass.
            ValueError: If a skill with the same name is already registered.
        """
        if not isinstance(skill, BaseSkill):
            raise TypeError(f"Expected BaseSkill instance, got {type(skill).__name__}")

        name = skill.name
        if name in self._skills:
            raise ValueError(
                f"Skill '{name}' is already registered "
                f"(existing: {self._skills[name]}). "
                f"Unregister first or use a unique name."
            )

        self._skills[name] = skill
        # Run one-time setup
        try:
            skill.setup()
        except Exception as e:
            logger.warning("Skill '%s' setup() failed: %s", name, e)

        logger.info("Registered skill: %s v%s", name, skill.version)

    def unregister(self, name: str) -> Optional[BaseSkill]:
        """Unregister a skill by name.

        Args:
            name: The skill name to remove.

        Returns:
            The removed skill instance, or None if not found.
        """
        skill = self._skills.pop(name, None)
        if skill:
            try:
                skill.teardown()
            except Exception as e:
                logger.warning("Skill '%s' teardown() failed: %s", name, e)
            logger.info("Unregistered skill: %s", name)
        return skill

    # ── Lookup ───────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[BaseSkill]:
        """Get a registered skill by name.

        Args:
            name: The skill name.

        Returns:
            The skill instance, or None if not registered.
        """
        return self._skills.get(name)

    def list_all(self) -> List[BaseSkill]:
        """Return all registered skills as a list."""
        return list(self._skills.values())

    def list_names(self) -> List[str]:
        """Return all registered skill names."""
        return list(self._skills.keys())

    def has(self, name: str) -> bool:
        """Check if a skill is registered."""
        return name in self._skills

    def count(self) -> int:
        """Return number of registered skills."""
        return len(self._skills)

    # ── Auto-discovery ───────────────────────────────────────────────────────

    def discover(self) -> int:
        """Auto-discover and register all BaseSkill subclasses in the builtin package.

        Scans ``tradingagents.skills.builtin`` only — external package_path is
        not accepted, preventing whitelist bypass (W7-1).

        Returns:
            Number of newly registered skills.
        """
        package_path = "tradingagents.skills.builtin"

        count = 0
        try:
            package = importlib.import_module(package_path)
        except ImportError:
            logger.warning("Cannot import discovery package: %s", package_path)
            return 0

        pkg_file = getattr(package, "__file__", None)
        if not pkg_file:
            logger.warning("Package '%s' has no __file__; cannot scan", package_path)
            return 0
        package_dir = os.path.dirname(pkg_file)
        for _importer, modname, _ispkg in pkgutil.iter_modules([package_dir]):
            full_modname = f"{package_path}.{modname}"
            try:
                module = importlib.import_module(full_modname)
            except Exception as e:
                logger.warning("Failed to import %s: %s", full_modname, e)
                continue

            # Find all concrete BaseSkill subclasses in the module
            for _attr_name, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, BaseSkill)
                    and obj is not BaseSkill
                    and not inspect.isabstract(obj)
                ):
                    # Avoid duplicate registration
                    try:
                        instance = obj()
                        if not self.has(instance.name):
                            self.register(instance)
                            count += 1
                    except Exception as e:
                        logger.warning(
                            "Failed to instantiate/register %s: %s",
                            obj.__name__, e,
                        )

        self._discovered = True
        logger.info("Discovery complete: %d new skills registered", count)
        return count

    # ── Filtering ────────────────────────────────────────────────────────────

    def filter_by_regime(self, regime) -> List[BaseSkill]:
        """Return skills that support the given market regime.

        Args:
            regime: MarketRegime enum value.

        Returns:
            List of skills that declare support for this regime.
        """
        from tradingagents.skills.base_skill import MarketRegime

        if isinstance(regime, str):
            try:
                regime = MarketRegime(regime)
            except ValueError:
                regime = MarketRegime.UNKNOWN

        return [
            skill for skill in self._skills.values()
            if regime in skill.supported_regimes or MarketRegime.UNKNOWN in skill.supported_regimes
        ]

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, dict]:
        """Return registry state as a dict for debugging."""
        return {name: skill.to_info() for name, skill in self._skills.items()}

    def __repr__(self) -> str:
        names = ", ".join(self._skills.keys()) or "(empty)"
        return f"SkillRegistry({len(self._skills)} skills: {names})"
