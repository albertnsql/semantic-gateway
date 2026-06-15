"""
gateway/cache.py — Intent-keyed in-memory query result cache with TTL eviction.

Replaces the ad-hoc raw-string cache in query.py with a structured cache
that keys on the serialised QueryIntent dict.  Two differently-worded questions
that resolve to the same intent are now served from the same cache entry.

Fix applied:
  - Added LRU-style maxsize cap (default 500 entries) backed by OrderedDict.
    Previously the cache could grow without bound; now the oldest entry is
    evicted whenever the store exceeds maxsize.

Usage::

    cache = QueryCache(ttl_seconds=3600, maxsize=500)
    result = cache.get(intent_dict)     # None on miss or expired
    cache.set(intent_dict, result_dict)

"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import decimal
import datetime
from collections import OrderedDict
from typing import Optional

def make_json_safe(obj):
    if isinstance(obj, list):
        return [make_json_safe(i) for i in obj]
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    return obj

logger = logging.getLogger(__name__)


class QueryCache:
    """
    TTL-based in-memory cache keyed on serialised query intent dicts.

    Uses an OrderedDict so that the oldest entry can be evicted in O(1)
    when the store reaches ``maxsize``.  Expired entries are lazily
    removed on read (get) to keep the hot path fast.
    """

    def __init__(self, ttl_seconds: int = 3600, maxsize: int = 500, disk_path: Optional[str] = None) -> None:
        """
        Initialise with a configurable TTL, maximum entry count, and optional disk persistence.

        Args:
            ttl_seconds: How long a cached result remains valid (default: 1 hour).
            maxsize:     Maximum number of entries before oldest is evicted (default: 500).
            disk_path:   Optional filepath to persist the cache to disk.
        """
        self._store: OrderedDict = OrderedDict()
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._disk_path = disk_path
        self._load()

    def _load(self) -> None:
        if self._disk_path and os.path.exists(self._disk_path):
            try:
                with open(self._disk_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        self._store[k] = v
                logger.info("Loaded %d entries from disk cache at %s", len(self._store), self._disk_path)
            except Exception as e:
                logger.warning("Failed to load disk cache from %s: %s", self._disk_path, e)

    def _save(self) -> None:
        if self._disk_path:
            try:
                with open(self._disk_path, "w", encoding="utf-8") as f:
                    json.dump(self._store, f)
            except Exception as e:
                logger.warning("Failed to save disk cache to %s: %s", self._disk_path, e)

    # ──────────────────────────────────────────────── public API

    def _make_key(self, intent: dict) -> str:
        """Return a stable SHA-256 key for an intent dict (sorted keys)."""
        serialised = json.dumps(intent, sort_keys=True, default=str)
        return hashlib.sha256(serialised.encode()).hexdigest()

    def get(self, intent: dict) -> Optional[dict]:
        """Return the cached result for intent, or None if missing/expired."""
        key = self._make_key(intent)
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.time() > entry["expires_at"]:
            del self._store[key]
            logger.debug("Cache EXPIRED for key %s…", key[:8])
            return None
        # Move to end (most recently used) to preserve LRU ordering
        self._store.move_to_end(key)
        logger.info("Cache HIT for key %s…", key[:8])
        return entry["result"]

    def set(self, intent: dict, result: dict) -> None:
        """
        Store a result under the intent key with TTL expiry.

        If the store already holds this key, it is refreshed in place.
        If the store exceeds maxsize after insertion, the oldest entry
        (least recently used) is evicted.
        """
        key = self._make_key(intent)
        now = time.time()

        if key in self._store:
            # Refresh in place — move to end to mark as most recently used
            self._store.move_to_end(key)

        result = make_json_safe(result)

        self._store[key] = {
            "result": result,
            "expires_at": now + self._ttl,
            "cached_at": now,
        }

        # Evict oldest entry if over capacity
        if len(self._store) > self._maxsize:
            evicted_key, _ = self._store.popitem(last=False)
            logger.debug("Cache EVICT (LRU) for key %s… (maxsize=%d)", evicted_key[:8], self._maxsize)

        logger.info("Cache SET for key %s… (entries=%d/%d)", key[:8], len(self._store), self._maxsize)
        self._save()

    def invalidate(self, intent: dict) -> None:
        """Remove the cached entry for this intent if it exists."""
        key = self._make_key(intent)
        self._store.pop(key, None)
        self._save()

    def clear(self) -> None:
        """Evict all cache entries."""
        self._store.clear()
        self._save()
        logger.info("Query cache cleared.")

    def stats(self) -> dict:
        """Return basic cache statistics."""
        now = time.time()
        active = sum(1 for e in self._store.values() if e["expires_at"] > now)
        return {
            "total_entries": len(self._store),
            "active_entries": active,
            "expired_entries": len(self._store) - active,
            "ttl_seconds": self._ttl,
            "maxsize": self._maxsize,
        }
