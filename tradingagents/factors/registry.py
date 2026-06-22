"""Alpha registry: AST-scan zoo modules, validate metadata, lazy-import on compute.

Ported from Vibe-Trading with import path adaptation for TAF.

Design contract:
    AlphaMeta (pydantic, ``extra="forbid", frozen=True``)
    Registry.list(zoo=None, theme=None, universe=None) -> list[str]
    Registry.get(alpha_id) -> Alpha
    Registry.compute(alpha_id, panel) -> pd.DataFrame
    Registry.health() -> dict   # {loaded, failed, errors}
    Registry.load_alpha_meta_from_py(path) -> AlphaMeta  # AST, no import
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tradingagents.factors.base import Alpha, rank

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MAX_PY_BYTES = 200_000

Theme = Literal[
    "momentum",
    "reversal",
    "volume",
    "volatility",
    "quality",
    "value",
    "liquidity",
    "microstructure",
    "sentiment",
    "growth",
    "leverage",
]

PanelColumn = Literal["open", "high", "low", "close", "volume", "vwap", "amount"]

Universe = Literal["equity_us", "equity_cn", "equity_hk", "crypto", "futures"]


class AlphaMeta(BaseModel):
    """Strict metadata schema; matches the ``__alpha_meta__`` dict literal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9]+_[a-z0-9_]+$")
    nickname: str | None = None
    theme: list[Theme]
    formula_latex: str
    columns_required: list[PanelColumn]
    extras_required: list[str] = Field(default_factory=list)
    requires_sector: bool = False
    universe: list[Universe]
    frequency: list[str]
    decay_horizon: int = Field(ge=0, le=60)
    min_warmup_bars: int = Field(ge=0)
    notes: str = ""


class SkipAlpha(Exception):
    """Raised when an alpha's preconditions (sector, columns) are not met."""


class RegistryError(Exception):
    """Raised on registry-level configuration errors."""


@dataclass(frozen=True, slots=True)
class _LoadError:
    alpha_id: str
    reason: str


def _validate_id_token(token: str, kind: str) -> None:
    if not _ID_RE.fullmatch(token):
        raise RegistryError(f"invalid {kind} {token!r}: must match {_ID_RE.pattern}")


def load_alpha_meta_from_py(path: Path) -> AlphaMeta:
    """AST-extract the ``__alpha_meta__`` dict literal from a zoo module.

    No import is performed — purely static parsing.
    """
    size = path.stat().st_size
    if size > _MAX_PY_BYTES:
        raise RegistryError(f"{path.name}: {size}B exceeds {_MAX_PY_BYTES}B cap")

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    meta_node: ast.expr | None = None
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
        if any(t.id == "__alpha_meta__" for t in targets):
            meta_node = stmt.value
            break

    if meta_node is None:
        raise RegistryError(f"{path.name}: __alpha_meta__ assignment not found")

    try:
        raw = ast.literal_eval(meta_node)
    except (ValueError, SyntaxError) as exc:
        raise RegistryError(f"{path.name}: __alpha_meta__ not a literal: {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryError(f"{path.name}: __alpha_meta__ must be dict, got {type(raw).__name__}")

    try:
        return AlphaMeta(**raw)
    except ValidationError as exc:
        raise RegistryError(f"{path.name}: AlphaMeta validation failed: {exc}") from exc


def _zoo_dir_default() -> Path:
    return Path(__file__).parent / "zoo"


class Registry:
    """In-memory registry of all discoverable alphas across zoo subdirectories."""

    def __init__(self, zoo_root: Path | None = None) -> None:
        default_root = _zoo_dir_default()
        self._zoo_root = (zoo_root or default_root).resolve()
        self._use_filesystem_loader = self._zoo_root != default_root.resolve()
        self._py_paths: dict[str, Path] = {}
        self._alphas: dict[str, Alpha] = {}
        self._load_errors: list[_LoadError] = []
        self._scan()

    # ------------------------- scanning -------------------------

    def _scan(self) -> None:
        if not self._zoo_root.is_dir():
            return
        for zoo_dir in sorted(self._zoo_root.iterdir()):
            if not zoo_dir.is_dir():
                continue
            zoo_id = zoo_dir.name
            if zoo_id.startswith("_") or zoo_id == "__pycache__":
                continue
            try:
                _validate_id_token(zoo_id, "zoo_id")
            except RegistryError as exc:
                self._load_errors.append(_LoadError(zoo_id, str(exc)))
                continue
            for py_file in sorted(zoo_dir.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                self._try_register(zoo_id, py_file)

    def _try_register(self, zoo_id: str, py_file: Path) -> None:
        short_id = py_file.stem
        try:
            _validate_id_token(short_id, "alpha_id_short")
        except RegistryError as exc:
            self._load_errors.append(_LoadError(f"{zoo_id}.{short_id}", str(exc)))
            return

        try:
            meta = load_alpha_meta_from_py(py_file)
        except RegistryError as exc:
            self._load_errors.append(_LoadError(f"{zoo_id}.{short_id}", str(exc)))
            return

        module_path = f"tradingagents.factors.zoo.{zoo_id}.{short_id}"
        alpha = Alpha(id=meta.id, zoo=zoo_id, module_path=module_path, meta=meta.model_dump())
        if alpha.id in self._alphas:
            self._load_errors.append(_LoadError(alpha.id, "duplicate alpha id"))
            return
        self._alphas[alpha.id] = alpha
        self._py_paths[alpha.id] = py_file

    # ------------------------- public API -------------------------

    def list(
        self,
        zoo: str | None = None,
        theme: str | None = None,
        universe: str | None = None,
    ) -> list[str]:
        """Return alpha IDs matching the (optional) filters."""
        out: list[str] = []
        for a in self._alphas.values():
            if zoo is not None and a.zoo != zoo:
                continue
            if theme is not None and theme not in a.meta.get("theme", []):
                continue
            if universe is not None and universe not in a.meta.get("universe", []):
                continue
            out.append(a.id)
        return sorted(out)

    def get(self, alpha_id: str) -> Alpha:
        if alpha_id not in self._alphas:
            raise KeyError(f"alpha_id {alpha_id!r} not in registry")
        return self._alphas[alpha_id]

    def health(self) -> dict[str, Any]:
        return {
            "loaded": len(self._alphas),
            "failed": len(self._load_errors),
            "errors": [
                {"alpha_id": e.alpha_id, "reason": e.reason} for e in self._load_errors
            ],
        }

    def compute(self, alpha_id: str, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """Lazy-import the alpha module and run its ``compute(panel)``.

        Raises:
            KeyError: alpha_id unknown.
            SkipAlpha: required column / sector tag absent in panel.
            RegistryError: import/compute failed or output failed sanity checks.
        """
        alpha = self.get(alpha_id)
        meta = alpha.meta

        missing = [c for c in meta.get("columns_required", []) if c not in panel]
        if missing:
            raise SkipAlpha(f"{alpha_id}: panel missing required columns {missing}")
        missing_extra = [c for c in meta.get("extras_required", []) if c not in panel]
        if missing_extra:
            raise SkipAlpha(f"{alpha_id}: panel missing extras {missing_extra}")
        if meta.get("requires_sector") and "sector" not in panel:
            raise SkipAlpha(f"{alpha_id}: panel missing sector tag")

        try:
            module = self._load_module(alpha)
        except Exception as exc:
            raise RegistryError(f"{alpha_id}: import failed: {exc}") from exc

        compute_fn = getattr(module, "compute", None)
        if compute_fn is None:
            raise RegistryError(f"{alpha_id}: module has no compute() function")

        try:
            result = compute_fn(panel)
        except Exception as exc:
            raise RegistryError(f"{alpha_id}: compute() raised: {exc}") from exc

        return self._validate_output(alpha_id, result, panel)

    def _load_module(self, alpha: Alpha) -> ModuleType:
        if not self._use_filesystem_loader:
            return importlib.import_module(alpha.module_path)
        py_file = self._py_paths[alpha.id]
        cached = sys.modules.get(alpha.module_path)
        if cached is not None and getattr(cached, "__file__", None) == str(py_file):
            return cached
        spec = importlib.util.spec_from_file_location(alpha.module_path, py_file)
        if spec is None or spec.loader is None:
            raise RegistryError(f"{alpha.id}: could not build import spec for {py_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[alpha.module_path] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(alpha.module_path, None)
            raise
        return module

    @staticmethod
    def _validate_output(
        alpha_id: str,
        result: Any,
        panel: dict[str, pd.DataFrame],
    ) -> pd.DataFrame:
        if not isinstance(result, pd.DataFrame):
            raise RegistryError(
                f"{alpha_id}: compute() returned {type(result).__name__}, expected DataFrame"
            )
        ref = panel.get("close")
        if ref is not None and result.shape != ref.shape:
            raise RegistryError(
                f"{alpha_id}: output shape {result.shape} != close shape {ref.shape}"
            )
        arr = result.to_numpy(dtype=np.float64, na_value=np.nan)
        if np.isinf(arr).any():
            raise RegistryError(f"{alpha_id}: output contains +/- inf")
        nan_ratio = float(np.isnan(arr).mean()) if arr.size > 0 else 1.0
        if nan_ratio > 0.95:
            raise RegistryError(f"{alpha_id}: output >95% NaN (nan_ratio={nan_ratio:.3f})")
        return result

    def compute_batch(
        self,
        alpha_ids: list[str],
        panel: dict[str, pd.DataFrame],
    ) -> dict[str, pd.DataFrame]:
        """Compute multiple alphas, skipping failures gracefully.

        Returns:
            Dict of alpha_id -> result DataFrame (only successful computations).
        """
        results: dict[str, pd.DataFrame] = {}
        for aid in alpha_ids:
            try:
                results[aid] = self.compute(aid, panel)
            except (SkipAlpha, RegistryError, KeyError) as exc:
                logger.debug("Skipping alpha %s: %s", aid, exc)
        return results

    def compute_ic(
        self,
        alpha_id: str,
        panel: dict[str, pd.DataFrame],
        forward_returns: pd.DataFrame,
    ) -> float:
        """Compute Information Coefficient (rank IC) for one alpha.

        Returns:
            Mean Pearson rank correlation across dates.
        """
        factor = self.compute(alpha_id, panel)
        # Align columns
        common = factor.columns.intersection(forward_returns.columns)
        if common.empty:
            return 0.0
        f = rank(factor[common])
        r = rank(forward_returns[common])
        # Per-date Pearson correlation on ranked data (≈ Spearman)
        ic_series = f.corrwith(r, axis=1, method="pearson")
        return float(ic_series.mean()) if not ic_series.empty else 0.0


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_registry_cache: "Registry | None" = None
_registry_cache_lock = threading.Lock()


def get_default_registry() -> Registry:
    """Return a process-wide cached ``Registry`` for the bundled zoo."""
    global _registry_cache
    with _registry_cache_lock:
        if _registry_cache is None:
            _registry_cache = Registry()
        return _registry_cache


def reset_default_registry() -> None:
    """Drop the cached registry (test hook)."""
    global _registry_cache
    with _registry_cache_lock:
        _registry_cache = None
