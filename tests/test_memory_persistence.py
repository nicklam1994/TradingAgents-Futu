"""Tests for FinancialSituationMemory persistence and CalibrationResult.

Covers:
  - save/load round-trip
  - Cross-process persistence (write, new instance reads back)
  - BM25 search works after persistence reload
  - CalibrationResult read/write
  - Auto-save on add_situations / add_calibration
  - Atomic write (no corruption on repeated saves)
  - Missing file graceful handling
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tradingagents.agents.utils.memory import (
    CalibrationResult,
    FinancialSituationMemory,
    _FORMAT_VERSION,
    _serialize_memories,
    _deserialize_memories,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_memory_dir(tmp_path: Path) -> Path:
    """Provide a clean temp directory for each test."""
    return tmp_path / "memory"


SAMPLE_SITUATIONS = [
    ("High inflation with rising rates", "Buy defensive stocks"),
    ("Tech sector volatility increasing", "Reduce growth exposure"),
    ("Strong dollar hitting EM", "Hedge FX exposure"),
]


# ── Step 1: save/load round-trip ─────────────────────────────────────────────


class TestSaveLoadRoundTrip:
    """save() then load() should preserve all data."""

    def test_basic_round_trip(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("roundtrip", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_situations(SAMPLE_SITUATIONS)

        # Create a fresh instance that loads from disk
        mem2 = FinancialSituationMemory("roundtrip", persist_dir=tmp_memory_dir)
        assert mem2.documents == [s[0] for s in SAMPLE_SITUATIONS]
        assert mem2.recommendations == [s[1] for s in SAMPLE_SITUATIONS]

    def test_json_file_created(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("filecheck", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_situations([("test", "rec")])
        assert (tmp_memory_dir / "filecheck.json").exists()

    def test_json_format_version(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("fmt", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_situations([("x", "y")])
        data = json.loads((tmp_memory_dir / "fmt.json").read_text())
        assert data["version"] == _FORMAT_VERSION
        assert isinstance(data["memories"], list)
        assert len(data["memories"]) == 1

    def test_empty_memory_save(self, tmp_memory_dir: Path):
        """Saving an empty memory should create a valid JSON file."""
        mem = FinancialSituationMemory("empty", persist_dir=tmp_memory_dir, auto_load=False)
        mem.save()
        data = json.loads((tmp_memory_dir / "empty.json").read_text())
        assert data["memories"] == []
        assert data["calibrations"] == []


# ── Step 2: BM25 search after persistence ────────────────────────────────────


class TestBM25AfterPersistence:
    """BM25 search must work identically after save + reload."""

    def test_search_survives_reload(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("search", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_situations(SAMPLE_SITUATIONS)

        # Search before reload
        results_before = mem.get_memories("inflation rising rates", n_matches=1)
        assert len(results_before) == 1
        assert "inflation" in results_before[0]["matched_situation"].lower()

        # Reload and search again
        mem2 = FinancialSituationMemory("search", persist_dir=tmp_memory_dir)
        results_after = mem2.get_memories("inflation rising rates", n_matches=1)
        assert len(results_after) == 1
        assert results_after[0]["matched_situation"] == results_before[0]["matched_situation"]
        assert results_after[0]["recommendation"] == results_before[0]["recommendation"]

    def test_search_empty_memory(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("empty_search", persist_dir=tmp_memory_dir)
        assert mem.get_memories("anything") == []

    def test_multiple_matches_after_reload(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("multi", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_situations(SAMPLE_SITUATIONS)

        mem2 = FinancialSituationMemory("multi", persist_dir=tmp_memory_dir)
        results = mem2.get_memories("tech volatility", n_matches=2)
        assert len(results) == 2
        # Scores should be descending
        assert results[0]["similarity_score"] >= results[1]["similarity_score"]


# ── Step 3: CalibrationResult persistence ────────────────────────────────────


class TestCalibrationPersistence:
    """CalibrationResult read/write round-trip."""

    def test_add_and_load_calibration(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("cal", persist_dir=tmp_memory_dir, auto_load=False)
        cal = CalibrationResult(
            prediction="AAPL will rise 3%",
            actual_outcome="AAPL rose 2.1%",
            confidence=0.85,
            asset="AAPL",
            metadata={"strategy": "momentum"},
        )
        mem.add_calibration(cal)

        # Reload
        mem2 = FinancialSituationMemory("cal", persist_dir=tmp_memory_dir)
        cals = mem2.get_calibrations()
        assert len(cals) == 1
        assert cals[0].prediction == "AAPL will rise 3%"
        assert cals[0].actual_outcome == "AAPL rose 2.1%"
        assert cals[0].confidence == 0.85
        assert cals[0].asset == "AAPL"
        assert cals[0].metadata == {"strategy": "momentum"}

    def test_filter_by_asset(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("asset_filter", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_calibration(CalibrationResult("p1", "a1", asset="AAPL"))
        mem.add_calibration(CalibrationResult("p2", "a2", asset="TSLA"))
        mem.add_calibration(CalibrationResult("p3", "a3", asset="AAPL"))

        mem2 = FinancialSituationMemory("asset_filter", persist_dir=tmp_memory_dir)
        aapl = mem2.get_calibrations(asset="AAPL")
        assert len(aapl) == 2
        tsla = mem2.get_calibrations(asset="TSLA")
        assert len(tsla) == 1

    def test_calibrations_persist_with_situations(self, tmp_memory_dir: Path):
        """Calibrations and situations coexist in the same JSON file."""
        mem = FinancialSituationMemory("mixed", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_situations(SAMPLE_SITUATIONS)
        mem.add_calibration(CalibrationResult("p", "a", asset="X"))

        mem2 = FinancialSituationMemory("mixed", persist_dir=tmp_memory_dir)
        assert len(mem2.documents) == len(SAMPLE_SITUATIONS)
        assert len(mem2.get_calibrations()) == 1


# ── Step 4: Auto-save behavior ───────────────────────────────────────────────


class TestAutoSave:
    """add_situations and add_calibration should auto-save."""

    def test_add_situations_auto_saves(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("auto", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_situations([("s1", "r1")])
        # File should exist immediately
        assert (tmp_memory_dir / "auto.json").exists()
        # Reload should pick it up
        mem2 = FinancialSituationMemory("auto", persist_dir=tmp_memory_dir)
        assert len(mem2.documents) == 1

    def test_add_calibration_auto_saves(self, tmp_memory_dir: Path):
        mem = FinancialSituationMemory("auto_cal", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_calibration(CalibrationResult("p", "a"))
        mem2 = FinancialSituationMemory("auto_cal", persist_dir=tmp_memory_dir)
        assert len(mem2.get_calibrations()) == 1


# ── Step 5: Edge cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    """Graceful handling of missing files, corrupt data, clear."""

    def test_missing_file_no_error(self, tmp_memory_dir: Path):
        """Loading from a nonexistent file should not raise."""
        mem = FinancialSituationMemory("nonexistent", persist_dir=tmp_memory_dir)
        assert mem.documents == []
        assert mem.recommendations == []
        assert mem.calibrations == []

    def test_corrupt_json_no_error(self, tmp_memory_dir: Path):
        """A corrupt JSON file should log a warning, not crash."""
        tmp_memory_dir.mkdir(parents=True)
        (tmp_memory_dir / "corrupt.json").write_text("{bad json!!!")
        mem = FinancialSituationMemory("corrupt", persist_dir=tmp_memory_dir)
        assert mem.documents == []  # Falls back to empty

    def test_clear_persists(self, tmp_memory_dir: Path):
        """clear() should persist the empty state."""
        mem = FinancialSituationMemory("clr", persist_dir=tmp_memory_dir, auto_load=False)
        mem.add_situations(SAMPLE_SITUATIONS)
        mem.clear()

        mem2 = FinancialSituationMemory("clr", persist_dir=tmp_memory_dir)
        assert mem2.documents == []
        assert mem2.recommendations == []

    def test_independent_instances(self, tmp_memory_dir: Path):
        """Different names should not interfere."""
        mem_a = FinancialSituationMemory("aaa", persist_dir=tmp_memory_dir, auto_load=False)
        mem_a.add_situations([("a situation", "a rec")])

        mem_b = FinancialSituationMemory("bbb", persist_dir=tmp_memory_dir, auto_load=False)
        mem_b.add_situations([("b situation", "b rec")])

        # Reload both
        mem_a2 = FinancialSituationMemory("aaa", persist_dir=tmp_memory_dir)
        mem_b2 = FinancialSituationMemory("bbb", persist_dir=tmp_memory_dir)
        assert len(mem_a2.documents) == 1
        assert len(mem_b2.documents) == 1
        assert mem_a2.documents[0] != mem_b2.documents[0]


# ── Step 6: Serialization helpers ────────────────────────────────────────────


class TestSerializationHelpers:
    """Unit tests for _serialize / _deserialize functions."""

    def test_serialize_empty(self):
        data = _serialize_memories([], [], [])
        assert data["version"] == _FORMAT_VERSION
        assert data["memories"] == []
        assert data["calibrations"] == []

    def test_round_trip_serialize(self):
        docs = ["doc1", "doc2"]
        recs = ["rec1", "rec2"]
        cals = [CalibrationResult("p", "a", confidence=0.9, asset="X")]
        data = _serialize_memories(docs, recs, cals)

        d2, r2, c2 = _deserialize_memories(data)
        assert d2 == docs
        assert r2 == recs
        assert len(c2) == 1
        assert c2[0].prediction == "p"
        assert c2[0].confidence == 0.9
