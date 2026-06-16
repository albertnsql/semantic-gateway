"""
core/sql_template_cache.py — Compiled SQL template cache for the AI Semantic Gateway.

Stores MetricFlow-compiled SQL keyed on (metrics, dimensions) WITHOUT a time range.
When a query arrives with the same metrics+dimensions but a different time range, the
MetricFlow subprocess (45 s) is skipped entirely — the template is retrieved and the
time filter is injected directly before Snowflake execution.

Cache hierarchy:
    L1  SQLTemplateCache  — metric+dim key, 24 h TTL  (this module)
    L2  QueryCache        — full intent key (inc. time range), 8 h TTL  (cache.py)

Flow:
    1. Incoming query: mrr × plan_type × last_6_months
    2. Template cache GET(mrr, plan_type) → HIT → inject dates → Snowflake (~3 s)
    3. Template cache MISS → run MetricFlow (45 s) → store template → Snowflake

Usage::

    tpl_cache = SQLTemplateCache(ttl_seconds=86400)
    template = tpl_cache.get(["mrr"], ["plan_type"])    # None on miss
    tpl_cache.set(["mrr"], ["plan_type"], sql_template)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# Sentinel tokens used for parameterization
_DATE_PLACEHOLDER_START = "{start_date}"
_DATE_PLACEHOLDER_END = "{end_date}"


class SQLTemplateCache:
    """
    TTL-based in-memory cache of MetricFlow-compiled SQL templates.

    Key:   canonical hash of sorted(metrics) + sorted(dimensions)  — no time range
    Value: SQL string with date literals replaced by placeholder tokens, plus the
           physical time-column name needed to re-inject the filter.

    The cache is deliberately kept simple (no Redis, no disk) so it adds zero
    infrastructure dependencies.  A 24 h TTL is appropriate because MetricFlow
    semantic model changes only happen on dbt deploys, which restart the gateway.
    """

    def __init__(self, ttl_seconds: int = 86400, maxsize: int = 200) -> None:
        """
        Args:
            ttl_seconds: How long a compiled template stays valid.  Default 24 h.
            maxsize:     Maximum entries before LRU eviction.  Default 200.
        """
        self._store: OrderedDict = OrderedDict()
        self._ttl = ttl_seconds
        self._maxsize = maxsize

    # ──────────────────────────────────────────────── key helpers

    @staticmethod
    def make_key(metrics: list[str], dimensions: list[str]) -> str:
        """
        Stable SHA-256 key from sorted metrics + sorted dimensions.
        Time range is intentionally excluded so one template serves all time windows.
        """
        payload = {
            "metrics": sorted(metrics),
            "dimensions": sorted(dimensions),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()

    # ──────────────────────────────────────────────── public API

    def get(
        self, metrics: list[str], dimensions: list[str]
    ) -> Optional[dict]:
        """
        Return the cached template entry, or None if missing / expired.

        Returns a dict with keys:
            ``sql_template``   — SQL string with `{start_date}` and `{end_date}` placeholders
            ``has_time_filter`` — bool; True if the template contains placeholders
        """
        key = self.make_key(metrics, dimensions)
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.time() > entry["expires_at"]:
            del self._store[key]
            logger.debug("SQLTemplateCache EXPIRED for key %s…", key[:8])
            return None
        self._store.move_to_end(key)
        logger.info(
            "SQLTemplateCache HIT for %s × %s (key %s…)",
            metrics, dimensions, key[:8],
        )
        return entry["template"]

    def set(
        self,
        metrics: list[str],
        dimensions: list[str],
        sql_template: str,
        has_time_filter: bool,
    ) -> None:
        """
        Store a compiled SQL template.

        Args:
            metrics:         Metric names for this template.
            dimensions:      Dimension names for this template.
            sql_template:    Compiled SQL with date literals replaced by {start_date} and {end_date}.
            has_time_filter: Whether the template includes a time-filter placeholder.
        """
        key = self.make_key(metrics, dimensions)
        now = time.time()

        if key in self._store:
            self._store.move_to_end(key)

        self._store[key] = {
            "template": {
                "sql_template": sql_template,
                "has_time_filter": has_time_filter,
            },
            "expires_at": now + self._ttl,
            "cached_at": now,
        }

        if len(self._store) > self._maxsize:
            evicted_key, _ = self._store.popitem(last=False)
            logger.debug(
                "SQLTemplateCache EVICT (LRU) for key %s… (maxsize=%d)",
                evicted_key[:8], self._maxsize,
            )

        logger.info(
            "SQLTemplateCache SET for %s × %s (key %s…, parameterizable=%s)",
            metrics, dimensions, key[:8], has_time_filter,
        )

    def invalidate(self, metrics: list[str], dimensions: list[str]) -> None:
        """Remove a specific template entry."""
        key = self.make_key(metrics, dimensions)
        self._store.pop(key, None)

    def clear(self) -> None:
        """Evict all template entries."""
        self._store.clear()
        logger.info("SQLTemplateCache cleared.")

    def stats(self) -> dict:
        """Return basic statistics."""
        now = time.time()
        active = sum(1 for e in self._store.values() if e["expires_at"] > now)
        return {
            "total_entries": len(self._store),
            "active_entries": active,
            "expired_entries": len(self._store) - active,
            "ttl_seconds": self._ttl,
            "maxsize": self._maxsize,
        }


# ──────────────────────────────────────────────── SQL template helpers (module-level)

def parameterize_sql_dates(sql: str, start_date: str, end_date: str) -> tuple[str, bool]:
    """
    Replace literal date strings with {start_date} and {end_date} placeholders.
    Returns (parameterized_sql, success_bool).
    """
    if start_date == end_date:
        logger.warning("parameterize_sql_dates: start_date == end_date (%s). Cannot safely parameterize without ambiguity. Forcing miss.", start_date)
        return sql, False

    # Targeted replacement: we replace occurrences of exactly 'YYYY-MM-DD'
    s_literal = f"'{start_date}'"
    e_literal = f"'{end_date}'"
    
    # We only consider it a success if we found both bounds in the SQL
    if s_literal not in sql or e_literal not in sql:
        return sql, False

    sql = sql.replace(s_literal, "'{start_date}'")
    sql = sql.replace(e_literal, "'{end_date}'")
    
    return sql, True

