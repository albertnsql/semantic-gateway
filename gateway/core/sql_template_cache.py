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
    tpl_cache.set(["mrr"], ["plan_type"], sql_template, has_time_filter=True)

Date parameterization
---------------------
MetricFlow generates date literals in several styles depending on adapter and version:

    Plain quoted literal       : '2026-03-18'
    CAST expression            : CAST('2026-03-18' AS DATE)
    Snowflake cast shorthand   : '2026-03-18'::DATE
    DATE() function            : DATE('2026-03-18')
    Unquoted keyword           : DATE 2026-03-18  (rare, kept for safety)

``parameterize_sql_dates`` normalises ALL of these into a single canonical form:

    {start_date}   /   {end_date}

``restore_sql_dates`` is the inverse: it replaces the placeholders back with
the requested date strings, using the same style that the original SQL used so
that the adapter is never given an unexpected format.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# Sentinel tokens used for parameterization
_DATE_PLACEHOLDER_START = "{start_date}"
_DATE_PLACEHOLDER_END = "{end_date}"

# ---------------------------------------------------------------------------
# Regex patterns for every MetricFlow date literal style
#
# Each pattern has exactly one capture group containing the bare YYYY-MM-DD
# value (without quotes / cast wrappers).
# ---------------------------------------------------------------------------

_DATE_RE = r"\d{4}-\d{2}-\d{2}"

# Order matters: most-specific patterns must come first so they are not
# partially matched by a shorter pattern.
_DATE_PATTERNS: list[re.Pattern[str]] = [
    # CAST('2026-03-18' AS DATE)
    re.compile(
        r"CAST\(\s*'(" + _DATE_RE + r")'\s+AS\s+DATE\s*\)",
        re.IGNORECASE,
    ),
    # '2026-03-18'::DATE  or  '2026-03-18'::date
    re.compile(
        r"'(" + _DATE_RE + r")'::(?:DATE|TIMESTAMP(?:_NTZ)?)",
        re.IGNORECASE,
    ),
    # DATE('2026-03-18')
    re.compile(
        r"DATE\(\s*'(" + _DATE_RE + r")'\s*\)",
        re.IGNORECASE,
    ),
    # Plain quoted literal  '2026-03-18'
    re.compile(
        r"'(" + _DATE_RE + r")'",
    ),
]

# When we write the placeholder back into the SQL, we keep the exact wrapper
# that was used in the original SQL so the Snowflake adapter is happy.
# The placeholder is embedded *inside* the canonical wrapper form.
_WRAPPER_CAST    = "CAST('{date}'  AS DATE)"   # restored to CAST('{d}' AS DATE)
_WRAPPER_COLCAST = "'{date}'::DATE"
_WRAPPER_DATEFN  = "DATE('{date}')"
_WRAPPER_PLAIN   = "'{date}'"


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
            ``sql_template``    — SQL string with `{start_date}` and `{end_date}` placeholders
            ``has_time_filter`` — bool; True if the template contains placeholders
            ``date_style``      — str; the MetricFlow wrapper style used during parameterization
                                  (one of 'cast', 'colcast', 'datefn', 'plain')
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
        date_style: str = "plain",
    ) -> None:
        """
        Store a compiled SQL template.

        Args:
            metrics:         Metric names for this template.
            dimensions:      Dimension names for this template.
            sql_template:    Compiled SQL with date literals replaced by {start_date} / {end_date}.
            has_time_filter: Whether the template includes a time-filter placeholder.
            date_style:      The wrapper style used ('cast', 'colcast', 'datefn', 'plain').
                             Stored so restore_sql_dates can reconstruct the exact original form.
        """
        key = self.make_key(metrics, dimensions)
        now = time.time()

        if key in self._store:
            self._store.move_to_end(key)

        self._store[key] = {
            "template": {
                "sql_template": sql_template,
                "has_time_filter": has_time_filter,
                "date_style": date_style,
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
            "SQLTemplateCache SET for %s × %s (key %s…, parameterizable=%s, style=%s)",
            metrics, dimensions, key[:8], has_time_filter, date_style,
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


def _detect_style(sql: str, date_str: str) -> str:
    """
    Detect which wrapper style MetricFlow used for a given date string.

    Checks patterns from most-specific to least-specific and returns a style
    identifier: 'cast' | 'colcast' | 'datefn' | 'plain'.
    Falls back to 'plain' if none match (safe default).
    """
    if re.search(
        r"CAST\(\s*'" + re.escape(date_str) + r"'\s+AS\s+DATE\s*\)",
        sql,
        re.IGNORECASE,
    ):
        return "cast"
    if re.search(
        r"'" + re.escape(date_str) + r"'::(?:DATE|TIMESTAMP(?:_NTZ)?)",
        sql,
        re.IGNORECASE,
    ):
        return "colcast"
    if re.search(
        r"DATE\(\s*'" + re.escape(date_str) + r"'\s*\)",
        sql,
        re.IGNORECASE,
    ):
        return "datefn"
    return "plain"


def _make_placeholder_expr(placeholder: str, style: str) -> str:
    """
    Wrap a placeholder token with the right cast expression so Snowflake
    does not receive a bare string where it expects a typed date.

    Args:
        placeholder: ``{start_date}`` or ``{end_date}``
        style:       One of 'cast', 'colcast', 'datefn', 'plain'

    Returns:
        Wrapped placeholder string, e.g. ``CAST('{start_date}' AS DATE)``
    """
    if style == "cast":
        return f"CAST('{placeholder}' AS DATE)"
    if style == "colcast":
        return f"'{placeholder}'::DATE"
    if style == "datefn":
        return f"DATE('{placeholder}')"
    # plain — just the quoted placeholder
    return f"'{placeholder}'"


def parameterize_sql_dates(
    sql: str,
    start_date: str,
    end_date: str,
) -> tuple[str, bool, str]:
    """
    Replace MetricFlow date literals with ``{start_date}`` / ``{end_date}``
    placeholder tokens.

    Handles all MetricFlow date expression styles:
    - ``CAST('YYYY-MM-DD' AS DATE)``
    - ``'YYYY-MM-DD'::DATE``  /  ``'YYYY-MM-DD'::TIMESTAMP_NTZ``
    - ``DATE('YYYY-MM-DD')``
    - ``'YYYY-MM-DD'``  (plain quoted literal)

    Args:
        sql:        Raw compiled SQL from MetricFlow.
        start_date: Start date string in ``YYYY-MM-DD`` format.
        end_date:   End date string in ``YYYY-MM-DD`` format.

    Returns:
        A 3-tuple ``(parameterized_sql, success, style)`` where:
        - ``parameterized_sql`` is the SQL with placeholders substituted in
        - ``success`` is True when at least one date was successfully replaced
        - ``style`` is the detected wrapper style ('cast'|'colcast'|'datefn'|'plain')

    Notes:
        - When ``start_date == end_date`` the substitution is skipped (ambiguous).
        - If neither date is found in the SQL the original SQL is returned with
          ``success=False`` and ``style='plain'``.
    """
    if start_date == end_date:
        logger.warning(
            "parameterize_sql_dates: start_date == end_date (%s). "
            "Cannot safely parameterize without ambiguity. Forcing miss.",
            start_date,
        )
        return sql, False, "plain"

    # Detect the exact style MetricFlow used so we can round-trip faithfully.
    # Use the start_date for detection (both dates always use the same style).
    style = _detect_style(sql, start_date)
    if style == "plain" and start_date not in sql and end_date not in sql:
        # Try detecting via the end_date in case start is absent
        style = _detect_style(sql, end_date)

    replaced_start = False
    replaced_end = False
    result_sql = sql

    # Build the pattern-specific replacement strings for this style
    start_placeholder_expr = _make_placeholder_expr(_DATE_PLACEHOLDER_START, style)
    end_placeholder_expr   = _make_placeholder_expr(_DATE_PLACEHOLDER_END,   style)

    # Choose the matching regex for the detected style and substitute
    for pattern in _DATE_PATTERNS:
        def _make_replacer(ph_start: str, ph_end: str, s_date: str, e_date: str):
            """Closure so loop variables are captured correctly."""
            def replacer(m: re.Match) -> str:
                bare = m.group(1)
                if bare == s_date:
                    return ph_start
                if bare == e_date:
                    return ph_end
                return m.group(0)  # unrelated date literal — leave intact
            return replacer

        new_sql = pattern.sub(
            _make_replacer(
                start_placeholder_expr,
                end_placeholder_expr,
                start_date,
                end_date,
            ),
            result_sql,
        )

        if new_sql != result_sql:
            result_sql = new_sql

    # Check whether we actually inserted placeholders
    replaced_start = _DATE_PLACEHOLDER_START in result_sql
    replaced_end   = _DATE_PLACEHOLDER_END   in result_sql

    if not replaced_start and not replaced_end:
        logger.warning(
            "parameterize_sql_dates: neither start_date (%s) nor end_date (%s) found "
            "in SQL after scanning all known MetricFlow date patterns. "
            "Storing template without time-filter parameterization.",
            start_date,
            end_date,
        )
        return sql, False, "plain"

    success = replaced_start and replaced_end
    if not success:
        logger.warning(
            "parameterize_sql_dates: only one date bound was found in SQL "
            "(start_found=%s, end_found=%s). Marking as non-parameterizable.",
            replaced_start,
            replaced_end,
        )
        return sql, False, "plain"

    logger.debug(
        "parameterize_sql_dates: SUCCESS (style=%s). Parameterized %s..%s.",
        style,
        start_date,
        end_date,
    )
    return result_sql, True, style


def restore_sql_dates(
    sql_template: str,
    start_date: str,
    end_date: str,
    style: str = "plain",
) -> str:
    """
    Inverse of ``parameterize_sql_dates``.

    Replaces ``{start_date}`` and ``{end_date}`` placeholders (in whichever
    wrapper form they were stored) with the actual requested date values.

    Args:
        sql_template: SQL with placeholder tokens.
        start_date:   Requested start date in ``YYYY-MM-DD`` format.
        end_date:     Requested end date in ``YYYY-MM-DD`` format.
        style:        Wrapper style stored alongside the template
                      ('cast'|'colcast'|'datefn'|'plain').

    Returns:
        Executable SQL with concrete date literals substituted in.
    """
    start_expr = _make_placeholder_expr(_DATE_PLACEHOLDER_START, style)
    end_expr   = _make_placeholder_expr(_DATE_PLACEHOLDER_END,   style)

    start_literal = _make_placeholder_expr(start_date, style)
    end_literal   = _make_placeholder_expr(end_date,   style)

    result = sql_template.replace(start_expr, start_literal)
    result = result.replace(end_expr,   end_literal)

    # Safety net: if placeholders survive (e.g. style mismatch), do a bare
    # token replacement so the query is always executable.
    result = result.replace(_DATE_PLACEHOLDER_START, start_date)
    result = result.replace(_DATE_PLACEHOLDER_END,   end_date)

    return result
