"""
tests/test_query_cache.py — Unit tests for gateway/cache.py (QueryCache).

Covers:
  - Cache miss on empty cache
  - Cache hit returns correct result
  - TTL expiry (expired entry returns None and is removed)
  - LRU eviction when maxsize is reached
  - Same-intent key deduplication (refresh rather than duplicate)
  - Key stability: equivalent intents with different key ordering hit same slot
  - clear() wipes all entries
  - invalidate() removes specific entry
  - stats() returns correct counts for active vs expired entries
  - Large intent dict (no crash, deterministic key)
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from cache import QueryCache


# ──────────────────────────────────────────────── Helpers

def _intent(metric: str = "mrr", dim: str = "plan_type") -> dict:
    return {"metric": metric, "dimension": dim, "time_range": "last_30_days"}


def _result(value: int = 42) -> dict:
    return {"rows": [{"value": value}], "row_count": 1}


# ──────────────────────────────────────────────── Cache miss / hit


class TestCacheMissAndHit:
    def test_empty_cache_returns_none(self):
        """get() on an empty cache must return None."""
        cache = QueryCache(ttl_seconds=60)
        assert cache.get(_intent()) is None

    def test_cache_hit_returns_stored_result(self):
        """After set(), get() must return the exact same result dict."""
        cache = QueryCache(ttl_seconds=60)
        intent = _intent()
        result = _result(100)
        cache.set(intent, result)
        assert cache.get(intent) == result

    def test_cache_hit_different_intent_returns_none(self):
        """An intent that was never stored must return None."""
        cache = QueryCache(ttl_seconds=60)
        cache.set(_intent("mrr"), _result(1))
        assert cache.get(_intent("ltv")) is None

    def test_cache_returns_most_recently_set_result(self):
        """Updating an existing intent should refresh the stored result."""
        cache = QueryCache(ttl_seconds=60)
        intent = _intent()
        cache.set(intent, _result(1))
        cache.set(intent, _result(999))
        assert cache.get(intent) == _result(999)


# ──────────────────────────────────────────────── TTL expiry


class TestTtlExpiry:
    def test_expired_entry_returns_none(self):
        """An entry past its TTL must be evicted on get() and return None."""
        cache = QueryCache(ttl_seconds=1)
        intent = _intent()
        cache.set(intent, _result())
        # Manually expire by patching time.time to return a far-future value
        with patch("cache.time") as mock_time:
            mock_time.time.return_value = time.time() + 9999
            result = cache.get(intent)
        assert result is None

    def test_expired_entry_is_removed_from_store(self):
        """After TTL expiry, the entry must be deleted from the internal store."""
        cache = QueryCache(ttl_seconds=1)
        intent = _intent()
        cache.set(intent, _result())
        with patch("cache.time") as mock_time:
            mock_time.time.return_value = time.time() + 9999
            cache.get(intent)
        assert cache.stats()["total_entries"] == 0

    def test_not_yet_expired_entry_is_returned(self):
        """An entry just barely within TTL must still be served."""
        cache = QueryCache(ttl_seconds=3600)
        intent = _intent()
        cache.set(intent, _result(77))
        assert cache.get(intent) == _result(77)


# ──────────────────────────────────────────────── LRU eviction


class TestLruEviction:
    def test_oldest_entry_evicted_when_maxsize_exceeded(self):
        """When maxsize=3 and we add a 4th entry, the oldest must be evicted."""
        cache = QueryCache(ttl_seconds=3600, maxsize=3)
        intents = [_intent(metric=f"metric_{i}") for i in range(4)]
        for i, intent in enumerate(intents):
            cache.set(intent, _result(i))

        # First entry (metric_0) should have been evicted
        assert cache.get(intents[0]) is None
        # Entries 1-3 must still be present
        for intent in intents[1:]:
            assert cache.get(intent) is not None

    def test_total_entries_never_exceeds_maxsize(self):
        """After many inserts, total_entries must not exceed maxsize."""
        maxsize = 5
        cache = QueryCache(ttl_seconds=3600, maxsize=maxsize)
        for i in range(20):
            cache.set(_intent(metric=f"m{i}"), _result(i))
        assert cache.stats()["total_entries"] <= maxsize

    def test_hitting_entry_moves_it_to_end_of_lru(self):
        """Accessing an entry refreshes its LRU position so it survives eviction."""
        cache = QueryCache(ttl_seconds=3600, maxsize=3)
        first = _intent("metric_first")
        cache.set(first, _result(0))                      # added first → oldest
        cache.set(_intent("metric_second"), _result(1))
        cache.set(_intent("metric_third"), _result(2))
        # Access 'first' to move it to "recently used"
        cache.get(first)
        # Add a 4th entry → should evict 'second' (now the oldest), not 'first'
        cache.set(_intent("metric_fourth"), _result(3))
        assert cache.get(first) is not None, "'first' should NOT have been evicted."

    def test_maxsize_one_always_evicts_previous(self):
        """With maxsize=1, every new insert evicts the previous entry."""
        cache = QueryCache(ttl_seconds=3600, maxsize=1)
        intent_a = _intent("a")
        intent_b = _intent("b")
        cache.set(intent_a, _result(1))
        cache.set(intent_b, _result(2))
        assert cache.get(intent_a) is None
        assert cache.get(intent_b) == _result(2)


# ──────────────────────────────────────────────── Key stability


class TestKeyStability:
    def test_key_order_independent(self):
        """Two intent dicts with same key-value pairs in different order must hit the same slot."""
        cache = QueryCache(ttl_seconds=60)
        intent_a = {"metric": "mrr", "dim": "plan_type", "time": "last_30_days"}
        intent_b = {"time": "last_30_days", "dim": "plan_type", "metric": "mrr"}
        cache.set(intent_a, _result(42))
        # intent_b is logically identical → must be a cache HIT
        assert cache.get(intent_b) == _result(42)

    def test_different_values_produce_different_keys(self):
        """Intents with different metric values must map to different keys."""
        cache = QueryCache(ttl_seconds=60)
        cache.set(_intent("mrr"), _result(1))
        assert cache.get(_intent("ltv")) is None

    def test_large_intent_dict_does_not_crash(self):
        """A deeply nested, large intent should hash cleanly with no error."""
        large_intent = {
            "metrics": ["mrr", "ltv", "churn_rate"],
            "dimensions": [f"dim_{i}" for i in range(50)],
            "filters": [{"col": f"c{i}", "op": "eq", "val": str(i)} for i in range(20)],
            "time_range": {"start": "2020-01-01", "end": "2024-12-31", "relative": "custom"},
        }
        cache = QueryCache(ttl_seconds=60)
        cache.set(large_intent, _result(0))
        assert cache.get(large_intent) == _result(0)


# ──────────────────────────────────────────────── clear / invalidate


class TestClearAndInvalidate:
    def test_clear_removes_all_entries(self):
        """clear() must leave the cache completely empty."""
        cache = QueryCache(ttl_seconds=60)
        for i in range(5):
            cache.set(_intent(metric=f"m{i}"), _result(i))
        cache.clear()
        assert cache.stats()["total_entries"] == 0
        for i in range(5):
            assert cache.get(_intent(metric=f"m{i}")) is None

    def test_invalidate_removes_specific_entry(self):
        """invalidate() must remove only the targeted entry."""
        cache = QueryCache(ttl_seconds=60)
        intent_a = _intent("mrr")
        intent_b = _intent("ltv")
        cache.set(intent_a, _result(1))
        cache.set(intent_b, _result(2))
        cache.invalidate(intent_a)
        assert cache.get(intent_a) is None
        assert cache.get(intent_b) == _result(2)

    def test_invalidate_nonexistent_key_is_safe(self):
        """invalidate() for a key that doesn't exist must not raise."""
        cache = QueryCache(ttl_seconds=60)
        cache.invalidate({"metric": "nonexistent"})  # must not raise


# ──────────────────────────────────────────────── stats()


class TestStats:
    def test_stats_empty_cache(self):
        """Stats on an empty cache should all be zero."""
        cache = QueryCache(ttl_seconds=60, maxsize=500)
        s = cache.stats()
        assert s["total_entries"] == 0
        assert s["active_entries"] == 0
        assert s["expired_entries"] == 0
        assert s["ttl_seconds"] == 60
        assert s["maxsize"] == 500

    def test_stats_with_active_entries(self):
        """active_entries should reflect entries not yet expired."""
        cache = QueryCache(ttl_seconds=3600, maxsize=10)
        for i in range(3):
            cache.set(_intent(metric=f"m{i}"), _result(i))
        s = cache.stats()
        assert s["total_entries"] == 3
        assert s["active_entries"] == 3
        assert s["expired_entries"] == 0

    def test_stats_distinguishes_expired_vs_active(self):
        """Expired entries must be counted as expired, not active, in stats."""
        cache = QueryCache(ttl_seconds=3600, maxsize=10)
        # Add 2 entries normally
        cache.set(_intent("mrr"), _result(1))
        cache.set(_intent("ltv"), _result(2))
        # Manually mark one as expired by back-dating its expires_at
        key = cache._make_key(_intent("mrr"))
        cache._store[key]["expires_at"] = time.time() - 1  # already expired

        s = cache.stats()
        assert s["total_entries"] == 2
        assert s["active_entries"] == 1
        assert s["expired_entries"] == 1
