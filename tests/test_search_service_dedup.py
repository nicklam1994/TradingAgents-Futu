"""Tests for SearchService in-flight dedup (race condition fix).

Covers:
- Basic dedup: first caller acquires slot, second caller waits and gets result
- Concurrent fan-out: N threads all wait for the same query, all get the result
- Timeout: waiter gets None when searcher exceeds 30s
- Ordering: result is visible to waiters even after inflight entry is deleted
- No duplicate search: second thread must NOT trigger a new provider call
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.dataflows.search_service import (
    SearchResponse,
    SearchService,
    _Result,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_service() -> SearchService:
    """Create a SearchService with no real providers."""
    return SearchService(cache_ttl=600)


def _ok_response(query: str = "test", provider: str = "mock") -> SearchResponse:
    """Build a successful SearchResponse."""
    return SearchResponse(
        query=query,
        results=[],
        provider=provider,
        success=True,
        search_time=0.01,
    )


def _fail_response(query: str = "test", provider: str = "mock") -> SearchResponse:
    """Build a failed SearchResponse."""
    return SearchResponse(
        query=query,
        results=[],
        provider=provider,
        success=False,
        error_message="provider down",
    )


# ── _Result wrapper ─────────────────────────────────────────────────────────


class TestResultHolder:
    """The _Result dataclass holds a response for cross-thread handoff."""

    def test_default_is_none(self):
        r = _Result()
        assert r.response is None

    def test_set_and_read(self):
        r = _Result()
        resp = _ok_response()
        r.response = resp
        assert r.response is resp

    def test_shared_reference_across_threads(self):
        """Two threads holding the same _Result see the same .response."""
        r = _Result()
        seen = []

        def reader():
            # Busy-wait until response is set
            for _ in range(500):
                if r.response is not None:
                    break
                time.sleep(0.001)
            seen.append(r.response)

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.01)  # let reader start waiting
        r.response = _ok_response("shared")
        t.join(timeout=2)
        assert len(seen) == 1
        assert seen[0].query == "shared"


# ── _dedup_or_acquire / _dedup_complete ──────────────────────────────────────


class TestDedupAcquire:
    """Unit tests for the dedup acquire/complete pair."""

    def test_first_caller_gets_none(self):
        """First caller to _dedup_or_acquire should get None (owns the slot)."""
        svc = _make_service()
        result = svc._dedup_or_acquire("k1")
        assert result is None

    def test_second_caller_waits_and_gets_result(self):
        """Second caller blocks until _dedup_complete, then gets the result."""
        svc = _make_service()
        resp = _ok_response()

        # First caller acquires
        assert svc._dedup_or_acquire("k1") is None

        waiter_result = []

        def waiter():
            waiter_result.append(svc._dedup_or_acquire("k1"))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)  # let waiter block on event.wait()

        # First caller completes
        svc._dedup_complete("k1", resp)

        t.join(timeout=2)
        assert len(waiter_result) == 1
        assert waiter_result[0] is resp

    def test_inflight_entry_cleaned_up_after_complete(self):
        """After _dedup_complete, the inflight entry should be removed."""
        svc = _make_service()
        svc._dedup_or_acquire("k1")
        svc._dedup_complete("k1", _ok_response())
        assert "k1" not in svc._inflight

    def test_complete_on_missing_key_is_noop(self):
        """Calling _dedup_complete for a key that doesn't exist should not raise."""
        svc = _make_service()
        svc._dedup_complete("nonexistent", _ok_response())  # no exception

    def test_timeout_returns_none(self):
        """If the searcher never completes, the waiter gets None after timeout."""
        svc = _make_service()
        svc._dedup_or_acquire("k1")

        # Patch event.wait to simulate instant timeout
        with patch.object(threading.Event, "wait", return_value=False):
            result = svc._dedup_or_acquire("k1")

        assert result is None


# ── Concurrent scenario ─────────────────────────────────────────────────────


class TestDedupConcurrency:
    """Multi-threaded tests to verify the race condition is fixed."""

    def test_multiple_waiters_all_get_result(self):
        """N threads waiting for the same query all receive the same result."""
        svc = _make_service()
        resp = _ok_response("AAPL")
        n_waiters = 10
        results = [None] * n_waiters
        barrier = threading.Barrier(n_waiters + 1)  # +1 for main thread

        def waiter(idx):
            # Acquire — this thread sees an existing inflight entry and waits
            barrier.wait(timeout=2)
            results[idx] = svc._dedup_or_acquire("dup_key")

        # First caller acquires the slot
        assert svc._dedup_or_acquire("dup_key") is None

        threads = [threading.Thread(target=waiter, args=(i,)) for i in range(n_waiters)]
        for t in threads:
            t.start()

        # Let all waiters reach the barrier (and thus enter event.wait)
        barrier.wait(timeout=2)
        time.sleep(0.05)

        # Complete — all waiters should receive the result
        svc._dedup_complete("dup_key", resp)

        for t in threads:
            t.join(timeout=3)

        for i, r in enumerate(results):
            assert r is resp, f"waiter {i} got {r!r} instead of the expected response"

    def test_no_duplicate_provider_call(self):
        """When dedup kicks in, the provider.search() must be called exactly once."""
        svc = _make_service()
        mock_provider = MagicMock()
        mock_provider.name = "mock"
        mock_provider.search.return_value = _ok_response("TSLA", "mock")

        # Inject the mock provider
        svc._providers["tavily"] = mock_provider

        # Patch _has_keys and _get_provider to use our mock
        with (
            patch.object(svc, "_has_keys", return_value=True),
            patch.object(svc, "_get_provider", return_value=mock_provider),
            patch(
                "tradingagents.dataflows.search_service._PROVIDER_PRIORITY",
                ["tavily"],
            ),
        ):
            results = [None] * 5

            def do_search(idx):
                results[idx] = svc.search("TSLA stock news", max_results=5)

            threads = [threading.Thread(target=do_search, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        # Provider.search should be called exactly once (dedup prevents duplicates)
        assert mock_provider.search.call_count == 1

        # All threads should get a successful response
        for i, r in enumerate(results):
            assert r is not None, f"thread {i} got None"
            assert r.success is True, f"thread {i} got failure: {r.error_message}"

    def test_result_visible_after_entry_deleted(self):
        """Waiter can read the result even after the inflight entry is deleted.

        This is the core race condition scenario: _dedup_complete deletes the
        dict entry, but the waiter holds a live _Result reference.
        """
        svc = _make_service()
        resp = _ok_response("race_test")

        # First caller acquires
        assert svc._dedup_or_acquire("race_key") is None

        waiter_result = []

        def waiter():
            # This will block on event.wait()
            waiter_result.append(svc._dedup_or_acquire("race_key"))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)  # let waiter enter event.wait()

        # Complete — this deletes the inflight entry
        svc._dedup_complete("race_key", resp)
        assert "race_key" not in svc._inflight  # entry is gone

        t.join(timeout=2)

        # But the waiter should still have the result via its local _Result ref
        assert len(waiter_result) == 1
        assert waiter_result[0] is resp
