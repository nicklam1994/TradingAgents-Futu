"""Financial situation memory using BM25 for lexical similarity matching.

Uses BM25 (Best Matching 25) algorithm for retrieval - no API calls,
no token limits, works offline with any LLM provider.

Persistence: memories are stored as JSON files under ~/.tradingagents/memory/.
Each add_memory() call auto-saves to disk, so a crash never loses data.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

# ── Default storage directory ────────────────────────────────────────────────
_DEFAULT_MEMORY_DIR = Path.home() / ".tradingagents" / "memory"

# ── Current on-disk format version ──────────────────────────────────────────
_FORMAT_VERSION = "1.0"


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class CalibrationResult:
    """Records an LLM calibration observation (predicted vs actual).

    Used to track how well the agent's predictions match reality over time,
    enabling calibration-aware decision making.
    """

    prediction: str
    """What the agent predicted (e.g. 'AAPL will rise 3%')."""

    actual_outcome: str
    """What actually happened (e.g. 'AAPL rose 2.1%')."""

    confidence: float = 0.0
    """Agent's confidence in the prediction (0.0-1.0)."""

    asset: str = ""
    """Ticker or asset symbol this relates to."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Arbitrary extra context (strategy name, timeframe, etc.)."""

    timestamp: float = field(default_factory=time.time)
    """Unix timestamp when this calibration was recorded."""


# ── Persistence helpers ──────────────────────────────────────────────────────


def _memory_path(name: str, directory: Optional[Path] = None) -> Path:
    """Return the JSON file path for a named memory instance."""
    d = directory or _DEFAULT_MEMORY_DIR
    return d / f"{name}.json"


def _serialize_memories(
    documents: List[str],
    recommendations: List[str],
    calibrations: List[CalibrationResult],
) -> dict:
    """Build the on-disk JSON structure."""
    memories = []
    for doc, rec in zip(documents, recommendations):
        memories.append(
            {
                "text": doc,
                "recommendation": rec,
                "timestamp": time.time(),
            }
        )
    return {
        "version": _FORMAT_VERSION,
        "memories": memories,
        "calibrations": [asdict(c) for c in calibrations],
    }


def _deserialize_memories(data: dict) -> Tuple[List[str], List[str], List[CalibrationResult]]:
    """Parse the on-disk JSON structure back into lists.

    Returns:
        (documents, recommendations, calibrations)
    """
    documents: List[str] = []
    recommendations: List[str] = []
    for entry in data.get("memories", []):
        documents.append(entry["text"])
        recommendations.append(entry.get("recommendation", ""))

    calibrations: List[CalibrationResult] = []
    for c in data.get("calibrations", []):
        calibrations.append(
            CalibrationResult(
                prediction=c["prediction"],
                actual_outcome=c["actual_outcome"],
                confidence=c.get("confidence", 0.0),
                asset=c.get("asset", ""),
                metadata=c.get("metadata", {}),
                timestamp=c.get("timestamp", 0.0),
            )
        )
    return documents, recommendations, calibrations


# ── Main class ───────────────────────────────────────────────────────────────


class FinancialSituationMemory:
    """Memory system for storing and retrieving financial situations using BM25.

    Supports JSON file persistence so memories survive process restarts.
    Every mutation (add_situations, add_calibration, clear) auto-saves to disk.
    """

    def __init__(
        self,
        name: str,
        config: Optional[dict] = None,
        persist_dir: Optional[str | Path] = None,
        auto_load: bool = True,
    ):
        """Initialize the memory system.

        Args:
            name: Name identifier for this memory instance (used as filename).
            config: Configuration dict (kept for API compatibility, not used for BM25).
            persist_dir: Custom directory for JSON files. Defaults to
                ~/.tradingagents/memory/.
            auto_load: If True, automatically load existing data from disk
                on construction. Set False to start with a blank slate.
        """
        self.name = name
        self.documents: List[str] = []
        self.recommendations: List[str] = []
        self.calibrations: List[CalibrationResult] = []
        self.bm25: Optional[BM25Okapi] = None

        # Resolve storage path
        self._persist_dir = Path(persist_dir) if persist_dir else _DEFAULT_MEMORY_DIR
        self._file_path = _memory_path(name, self._persist_dir)

        # Auto-load existing data
        if auto_load:
            self.load()

    # ── Tokenization ─────────────────────────────────────────────────────

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text for BM25 indexing.

        Simple whitespace + punctuation tokenization with lowercasing.
        """
        tokens = re.findall(r"\b\w+\b", text.lower())
        return tokens

    def _rebuild_index(self):
        """Rebuild the BM25 index after adding documents."""
        if self.documents:
            tokenized_docs = [self._tokenize(doc) for doc in self.documents]
            self.bm25 = BM25Okapi(tokenized_docs)
        else:
            self.bm25 = None

    # ── Persistence ──────────────────────────────────────────────────────

    def save(self) -> None:
        """Persist current state to the JSON file.

        Creates the parent directory if it doesn't exist. Uses an atomic
        write (write-to-temp + rename) to avoid corruption on crash.
        """
        self._persist_dir.mkdir(parents=True, exist_ok=True)

        payload = _serialize_memories(
            self.documents, self.recommendations, self.calibrations
        )
        tmp_path = self._file_path.with_suffix(".json.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            # Atomic rename (POSIX guarantees this is atomic on same filesystem)
            os.replace(str(tmp_path), str(self._file_path))
            logger.debug("Saved memory '%s' (%d entries) to %s", self.name, len(self.documents), self._file_path)
        except Exception:
            # Clean up temp file on failure
            tmp_path.unlink(missing_ok=True)
            raise

    def load(self) -> None:
        """Load state from the JSON file.

        If the file doesn't exist, the memory stays empty (no error).
        Rebuilds the BM25 index after loading.
        """
        if not self._file_path.exists():
            logger.debug("No persisted file for memory '%s' at %s", self.name, self._file_path)
            return

        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load memory '%s' from %s: %s", self.name, self._file_path, exc)
            return

        self.documents, self.recommendations, self.calibrations = _deserialize_memories(data)
        self._rebuild_index()
        logger.debug(
            "Loaded memory '%s': %d situations, %d calibrations from %s",
            self.name,
            len(self.documents),
            len(self.calibrations),
            self._file_path,
        )

    # ── Mutation ─────────────────────────────────────────────────────────

    def add_situations(self, situations_and_advice: List[Tuple[str, str]]):
        """Add financial situations and their corresponding advice.

        Automatically persists to disk after adding.

        Args:
            situations_and_advice: List of tuples (situation, recommendation)
        """
        for situation, recommendation in situations_and_advice:
            self.documents.append(situation)
            self.recommendations.append(recommendation)

        # Rebuild BM25 index with new documents
        self._rebuild_index()

        # Auto-persist
        self.save()

    def add_calibration(self, result: CalibrationResult) -> None:
        """Add a calibration observation (predicted vs actual).

        Automatically persists to disk after adding.

        Args:
            result: The CalibrationResult to record.
        """
        self.calibrations.append(result)
        self.save()

    def clear(self):
        """Clear all stored memories and persist the empty state."""
        self.documents = []
        self.recommendations = []
        self.calibrations = []
        self.bm25 = None
        self.save()

    # ── Retrieval ────────────────────────────────────────────────────────

    def get_memories(self, current_situation: str, n_matches: int = 1) -> List[dict]:
        """Find matching recommendations using BM25 similarity.

        Args:
            current_situation: The current financial situation to match against
            n_matches: Number of top matches to return

        Returns:
            List of dicts with matched_situation, recommendation, and similarity_score
        """
        if not self.documents or self.bm25 is None:
            return []

        # Tokenize query
        query_tokens = self._tokenize(current_situation)

        # Get BM25 scores for all documents
        scores = self.bm25.get_scores(query_tokens)

        # Get top-n indices sorted by score (descending)
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[
            :n_matches
        ]

        # Build results
        results = []
        max_score = max(scores) if max(scores) > 0 else 1  # Normalize scores

        for idx in top_indices:
            # Normalize score to 0-1 range for consistency
            normalized_score = scores[idx] / max_score if max_score > 0 else 0
            results.append(
                {
                    "matched_situation": self.documents[idx],
                    "recommendation": self.recommendations[idx],
                    "similarity_score": normalized_score,
                }
            )

        return results

    def get_calibrations(self, asset: Optional[str] = None) -> List[CalibrationResult]:
        """Retrieve calibration results, optionally filtered by asset.

        Args:
            asset: If provided, only return calibrations for this asset symbol.

        Returns:
            List of CalibrationResult objects.
        """
        if asset is None:
            return list(self.calibrations)
        return [c for c in self.calibrations if c.asset == asset]


# ── CLI example ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Example usage
    matcher = FinancialSituationMemory("test_memory")

    # Example data
    example_data = [
        (
            "High inflation rate with rising interest rates and declining consumer spending",
            "Consider defensive sectors like consumer staples and utilities. Review fixed-income portfolio duration.",
        ),
        (
            "Tech sector showing high volatility with increasing institutional selling pressure",
            "Reduce exposure to high-growth tech stocks. Look for value opportunities in established tech companies with strong cash flows.",
        ),
        (
            "Strong dollar affecting emerging markets with increasing forex volatility",
            "Hedge currency exposure in international positions. Consider reducing allocation to emerging market debt.",
        ),
        (
            "Market showing signs of sector rotation with rising yields",
            "Rebalance portfolio to maintain target allocations. Consider increasing exposure to sectors benefiting from higher rates.",
        ),
    ]

    # Add the example situations and recommendations
    matcher.add_situations(example_data)

    # Example query
    current_situation = """
    Market showing increased volatility in tech sector, with institutional investors
    reducing positions and rising interest rates affecting growth stock valuations
    """

    try:
        recommendations = matcher.get_memories(current_situation, n_matches=2)

        for i, rec in enumerate(recommendations, 1):
            print(f"\nMatch {i}:")
            print(f"Similarity Score: {rec['similarity_score']:.2f}")
            print(f"Matched Situation: {rec['matched_situation']}")
            print(f"Recommendation: {rec['recommendation']}")

    except Exception as e:
        print(f"Error during recommendation: {str(e)}")
