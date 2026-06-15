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

# Sentinel token written into the template WHERE clause in place of the date range.
# Must be a string that cannot appear in valid SQL.
_DATE_PLACEHOLDER_START = "__TEMPLATE_START_DATE__"
_DATE_PLACEHOLDER_END = "__TEMPLATE_END_DATE__"


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
            ``sql_template``   — SQL string with placeholder tokens for dates
            ``time_col``       — physical column used for the date filter (may be None
                                 if the original query had no time range)
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
        time_col: str | None,
        has_time_filter: bool,
    ) -> None:
        """
        Store a compiled SQL template.

        Args:
            metrics:         Metric names for this template.
            dimensions:      Dimension names for this template.
            sql_template:    Compiled SQL with date literals replaced by placeholders.
            time_col:        Physical column name used for the time filter.
            has_time_filter: Whether the template includes a time-filter placeholder.
        """
        key = self.make_key(metrics, dimensions)
        now = time.time()

        if key in self._store:
            self._store.move_to_end(key)

        self._store[key] = {
            "template": {
                "sql_template": sql_template,
                "time_col": time_col,
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
            "SQLTemplateCache SET for %s × %s (key %s…, time_col=%s)",
            metrics, dimensions, key[:8], time_col,
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

def strip_time_filter(sql: str) -> tuple[str, str | None]:
    """
    Remove time-range WHERE predicates from a MetricFlow-compiled SQL string and
    replace them with placeholder tokens.

    MetricFlow injects time filters as one of these patterns:
        WHERE col >= 'YYYY-MM-DD' AND col <= 'YYYY-MM-DD'
        WHERE col BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD'
        AND col >= 'YYYY-MM-DD' AND col <= 'YYYY-MM-DD'   (appended to existing WHERE)

    Detects the physical time column from the known date columns in the schema.

    Returns:
        (sql_with_placeholders, time_col_name)
        time_col_name is None when no time filter was found.
    """
    import re

    # Known physical date columns across all mart tables
    _TIME_COLS = [
        "period_month",
        "payment_date",
        "session_start",
        "event_timestamp",
        "signup_date",
    ]

    for col in _TIME_COLS:
        # Pattern: col >= 'date' AND col <= 'date'  (with optional leading AND/WHERE)
        pattern_gte_lte = re.compile(
            r"(?:AND\s+|WHERE\s+)?"
            + re.escape(col)
            + r"\s*>=\s*'[^']+'\s+AND\s+"
            + re.escape(col)
            + r"\s*<=\s*'[^']+'",
            re.IGNORECASE,
        )
        # Pattern: col BETWEEN 'date' AND 'date'
        pattern_between = re.compile(
            r"(?:AND\s+|WHERE\s+)?"
            + re.escape(col)
            + r"\s+BETWEEN\s+'[^']+'\s+AND\s+'[^']+'",
            re.IGNORECASE,
        )

        replacement = (
            f"{col} >= '{_DATE_PLACEHOLDER_START}' "
            f"AND {col} <= '{_DATE_PLACEHOLDER_END}'"
        )

        new_sql, n1 = pattern_gte_lte.subn(replacement, sql)
        if n1 > 0:
            logger.debug("strip_time_filter: replaced >= / <= on '%s'", col)
            return new_sql, col

        new_sql, n2 = pattern_between.subn(replacement, sql)
        if n2 > 0:
            logger.debug("strip_time_filter: replaced BETWEEN on '%s'", col)
            return new_sql, col

    # No time filter found — return SQL unchanged with no column
    return sql, None


def inject_time_filter(
    sql_template: str,
    time_col: str,
    start_date: str,
    end_date: str,
) -> str:
    """
    Replace placeholder tokens in a SQL template with real date literals,
    or inject a date filter into the WHERE clause if no placeholders exist.

    Injection strategy (when no placeholder is present):
        Find the first GROUP BY / ORDER BY / HAVING / LIMIT clause and insert
        the date predicate *before* it — not at the very end of the string.
        This prevents the classic "unexpected WHERE after GROUP BY" syntax error.

    Args:
        sql_template: SQL with placeholder tokens (from strip_time_filter), or
                      a clean SQL string with no time filter at all.
        time_col:     Physical column name to filter on.
        start_date:   Start date string (YYYY-MM-DD).
        end_date:     End date string (YYYY-MM-DD).

    Returns:
        Final SQL with real date literals injected.
    """
    import re

    date_filter = (
        f"{time_col} >= '{start_date}'"
        f" AND {time_col} <= '{end_date}'"
    )

    # ── Fast path: template already has placeholders ─────────────────────────
    if _DATE_PLACEHOLDER_START in sql_template:
        sql = sql_template.replace(_DATE_PLACEHOLDER_START, start_date)
        sql = sql.replace(_DATE_PLACEHOLDER_END, end_date)
        return sql

    # ── No placeholder: inject the filter before GROUP BY / ORDER BY ─────────
    # Find the first clause that terminates the WHERE region.
    # We match at a newline boundary so we don't accidentally match inside a
    # string literal or column alias.
    terminator_match = re.search(
        r"(?m)^\s*(GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT)\b",
        sql_template,
        re.IGNORECASE,
    )

    if terminator_match:
        insert_pos = terminator_match.start()
        sql_before = sql_template[:insert_pos].rstrip()
        sql_after  = sql_template[insert_pos:]

        # We must only check for WHERE in the outer query, not inside CTEs.
        last_from_idx = sql_before.upper().rfind(" FROM ")
        segment = sql_before[last_from_idx:] if last_from_idx != -1 else sql_before

        if re.search(r"\bWHERE\b", segment, re.IGNORECASE):
            # Existing WHERE in outer query — append as AND condition
            return sql_before + f"\n  AND {date_filter}\n" + sql_after
        else:
            # No WHERE yet in outer query — add one
            return sql_before + f"\nWHERE {date_filter}\n" + sql_after
    else:
        # No GROUP BY / ORDER BY found — safe to append at the very end
        sql = sql_template.rstrip().rstrip(";").rstrip()
        last_from_idx = sql.upper().rfind(" FROM ")
        segment = sql[last_from_idx:] if last_from_idx != -1 else sql
        
        if re.search(r"\bWHERE\b", segment, re.IGNORECASE):
            return sql + f"\n  AND {date_filter}"
        else:
            return sql + f"\nWHERE {date_filter}"

