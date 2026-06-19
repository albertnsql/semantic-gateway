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

Why two parameterization strategies?
-------------------------------------
MetricFlow grain-adjusts the dates it embeds in SQL before running them.
For a monthly-grain metric like ``mrr``:

    User request  : --start-time 2026-03-19  --end-time 2026-06-19
    SQL generated : WHERE period_month >= '2026-03-01'    ← first of March
                       AND period_month <  '2026-07-01'   ← first of NEXT month

The user-provided dates (2026-03-19, 2026-06-19) never appear literally in the SQL,
so a simple search-and-replace approach fails.

Strategy 1 – ``parameterize_sql_dates`` (user-date search):
    Works when MetricFlow embeds dates as-is (day-grain metrics).

Strategy 2 – ``parameterize_by_auto_extraction`` (SQL scan):
    Reads whatever dates MetricFlow actually wrote, identifies the start/end bounds
    by proximity to the time column, and replaces them.  Works for ALL grains.

On cache HIT, ``apply_grain_rounding`` converts the new user-requested dates to
the same grain-adjusted form before injecting them into the stored template.
"""

from __future__ import annotations

import datetime
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

# ---------------------------------------------------------------------------
# Grain configuration
#
# Maps each physical time column to its semantic time grain.  Used by
# ``apply_grain_rounding`` to adjust user-requested dates to grain boundaries
# before injecting them into a cached SQL template.
# ---------------------------------------------------------------------------

_TIME_COL_GRAIN: dict[str, str] = {
    "period_month":    "month",   # fct_mrr_monthly
    "cohort_month":    "month",   # dim_subscribers (cohort)
    "payment_date":    "day",     # fct_payments
    "session_start":   "day",     # fct_stream_sessions
    "signup_date":     "day",     # dim_subscribers
    "event_timestamp": "day",     # stg_recommendation_events
}


def apply_grain_rounding(
    date_str: str,
    time_col: str,
    is_start: bool,
) -> str:
    """
    Apply MetricFlow's grain rounding to a user-provided date string.

    MetricFlow normalises filter bounds to the grain period boundary before
    embedding them in SQL:

        month grain → start : first day of the month  (e.g. 2026-03-19 → 2026-03-01)
                      end   : first day of NEXT month  (e.g. 2026-06-19 → 2026-07-01)
                              (exclusive upper bound — ``< '2026-07-01'``)

        day grain   → unchanged  (MetricFlow embeds the user date directly)

    Args:
        date_str: User-provided date in ``YYYY-MM-DD`` format.
        time_col: Physical time column name (used to look up grain in _TIME_COL_GRAIN).
        is_start: True for the lower bound, False for the upper bound.

    Returns:
        Grain-adjusted date string in ``YYYY-MM-DD`` format, or ``date_str``
        unchanged on parse error or unknown time column.
    """
    grain = _TIME_COL_GRAIN.get(time_col, "day")

    try:
        d = datetime.date.fromisoformat(date_str)
    except ValueError:
        logger.warning("apply_grain_rounding: could not parse date '%s'.", date_str)
        return date_str

    if grain == "month":
        if is_start:
            return d.replace(day=1).isoformat()
        else:
            # Exclusive upper bound: first day of the next calendar month
            if d.month == 12:
                return d.replace(year=d.year + 1, month=1, day=1).isoformat()
            return d.replace(month=d.month + 1, day=1).isoformat()

    # day grain — pass through unchanged (MetricFlow embeds dates as-is)
    return date_str


# ---------------------------------------------------------------------------
# Placeholder wrapper helpers
# ---------------------------------------------------------------------------

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
    return f"'{placeholder}'"


# ---------------------------------------------------------------------------
# SQLTemplateCache
# ---------------------------------------------------------------------------

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
            ``date_style``      — str; the MetricFlow wrapper style ('cast'|'colcast'|'datefn'|'plain')
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


# ---------------------------------------------------------------------------
# SQL template helpers (module-level)
# ---------------------------------------------------------------------------

def parameterize_sql_dates(
    sql: str,
    start_date: str,
    end_date: str,
) -> tuple[str, bool, str]:
    """
    Replace MetricFlow date literals with ``{start_date}`` / ``{end_date}``
    placeholder tokens by searching for the *user-provided* date strings.

    Works when MetricFlow embeds dates without grain adjustment (typically
    day-grain metrics).  For monthly-grain metrics (e.g. ``mrr``) where
    MetricFlow rounds ``2026-03-19`` → ``2026-03-01``, use
    ``parameterize_by_auto_extraction`` instead.

    Args:
        sql:        Raw compiled SQL from MetricFlow.
        start_date: Start date string in ``YYYY-MM-DD`` format.
        end_date:   End date string in ``YYYY-MM-DD`` format.

    Returns:
        A 3-tuple ``(parameterized_sql, success, style)`` where:
        - ``parameterized_sql`` is the SQL with placeholders substituted in
        - ``success`` is True when both dates were successfully replaced
        - ``style`` is the detected wrapper style ('cast'|'colcast'|'datefn'|'plain')
    """
    if start_date == end_date:
        logger.warning(
            "parameterize_sql_dates: start_date == end_date (%s). "
            "Cannot safely parameterize without ambiguity. Forcing miss.",
            start_date,
        )
        return sql, False, "plain"

    style = _detect_style(sql, start_date)
    if style == "plain" and start_date not in sql and end_date not in sql:
        style = _detect_style(sql, end_date)

    start_placeholder_expr = _make_placeholder_expr(_DATE_PLACEHOLDER_START, style)
    end_placeholder_expr   = _make_placeholder_expr(_DATE_PLACEHOLDER_END,   style)

    def _make_replacer(ph_start: str, ph_end: str, s_date: str, e_date: str):
        def replacer(m: re.Match) -> str:
            bare = m.group(1)
            if bare == s_date:
                return ph_start
            if bare == e_date:
                return ph_end
            return m.group(0)
        return replacer

    result_sql = sql
    for pattern in _DATE_PATTERNS:
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

    replaced_start = _DATE_PLACEHOLDER_START in result_sql
    replaced_end   = _DATE_PLACEHOLDER_END   in result_sql

    if not replaced_start and not replaced_end:
        # Dates not found — caller should fall back to auto-extraction
        return sql, False, "plain"

    if not (replaced_start and replaced_end):
        logger.warning(
            "parameterize_sql_dates: only one date bound was found in SQL "
            "(start_found=%s, end_found=%s).",
            replaced_start,
            replaced_end,
        )
        return sql, False, "plain"

    logger.debug(
        "parameterize_sql_dates: SUCCESS (style=%s). Parameterized %s..%s.",
        style, start_date, end_date,
    )
    return result_sql, True, style


def parameterize_by_auto_extraction(
    sql: str,
    time_col: str,
) -> tuple[str, bool, str]:
    """
    Parameterize a MetricFlow SQL template by *extracting* the actual date
    literals it embedded — without relying on the user-provided dates.

    This handles the common case where MetricFlow grain-adjusts dates before
    writing them into SQL (e.g. monthly-grain ``mrr``: user supplies
    ``2026-03-19`` but MetricFlow writes ``2026-03-01``).

    Strategy:
      1. Look for dates that appear directly in comparison expressions with
         ``time_col`` (e.g. ``period_month >= '2026-03-01'``).  This avoids
         picking up unrelated date literals in the same WHERE clause.
      2. If fewer than 2 such dates are found, fall back to a proximity scan
         (all dates within ``_PROXIMITY`` characters of ``time_col``).
      3. Treat the lexicographically smallest as ``{start_date}`` and the
         largest as ``{end_date}``.
      4. Replace with properly wrapped placeholder expressions.

    Args:
        sql:      Compiled SQL from MetricFlow (with grain-adjusted dates).
        time_col: Physical time-column name for the primary metric.

    Returns:
        ``(parameterized_sql, success, style)``
    """
    if not time_col:
        logger.warning(
            "parameterize_by_auto_extraction: no time_col provided. Cannot extract."
        )
        return sql, False, "plain"

    # ── Strategy 1: look for dates in direct comparisons with time_col ───────
    # Matches patterns like:
    #   period_month >= '2026-03-01'
    #   period_month < CAST('2026-07-01' AS DATE)
    #   '2026-03-01' <= period_month
    _COMPARISON_OPS = r"(?:>=|<=|<|>|=)"
    direct_dates: set[str] = set()

    for pattern in _DATE_PATTERNS:
        # time_col [op] <date_expr>
        fwd = re.compile(
            r"\b" + re.escape(time_col) + r"\b"
            + r"\s*" + _COMPARISON_OPS + r"\s*"
            + pattern.pattern,
            re.IGNORECASE,
        )
        for m in fwd.finditer(sql):
            # group(1) from the date pattern — it's the LAST group in combined re
            # We need to re-match just the date part to extract it.
            date_m = pattern.search(m.group(0))
            if date_m:
                direct_dates.add(date_m.group(1))

        # <date_expr> [op] time_col  (reversed operand order)
        rev = re.compile(
            pattern.pattern
            + r"\s*" + _COMPARISON_OPS + r"\s*"
            + r"\b" + re.escape(time_col) + r"\b",
            re.IGNORECASE,
        )
        for m in rev.finditer(sql):
            date_m = pattern.search(m.group(0))
            if date_m:
                direct_dates.add(date_m.group(1))

    if len(direct_dates) >= 2:
        filter_dates = direct_dates
        logger.debug(
            "parameterize_by_auto_extraction: found %d dates via comparison-operator scan: %s",
            len(filter_dates), sorted(filter_dates),
        )
    else:
        # ── Strategy 2: proximity fallback ────────────────────────────────────
        # Collect all date occurrences with their byte offsets
        occurrences: list[tuple[int, str]] = []
        seen_spans: set[tuple[int, int]] = set()
        for pattern in _DATE_PATTERNS:
            for m in pattern.finditer(sql):
                span = (m.start(), m.end())
                if span not in seen_spans:
                    seen_spans.add(span)
                    occurrences.append((m.start(), m.group(1)))

        if not occurrences:
            logger.warning(
                "parameterize_by_auto_extraction: no date literals found in SQL (time_col=%s).",
                time_col,
            )
            return sql, False, "plain"

        tc_re = re.compile(r"\b" + re.escape(time_col) + r"\b", re.IGNORECASE)
        tc_positions = [m.start() for m in tc_re.finditer(sql)]
        _PROXIMITY = 500

        if tc_positions:
            filter_dates = {
                date_val
                for (date_pos, date_val) in occurrences
                for tc_pos in tc_positions
                if abs(date_pos - tc_pos) <= _PROXIMITY
            }
        else:
            filter_dates = {date_val for (_, date_val) in occurrences}

    if len(filter_dates) < 2:
        logger.warning(
            "parameterize_by_auto_extraction: fewer than 2 distinct dates found "
            "near time_col='%s' (found: %s).",
            time_col,
            sorted(filter_dates),
        )
        return sql, False, "plain"

    sorted_dates = sorted(filter_dates)
    start_date = sorted_dates[0]   # earliest  = lower bound
    end_date   = sorted_dates[-1]  # latest    = upper bound

    # ── Detect wrapper style ─────────────────────────────────────────────────
    style = _detect_style(sql, start_date)
    if style == "plain":
        style = _detect_style(sql, end_date)

    # ── Build placeholder expressions and substitute ──────────────────────────
    start_ph = _make_placeholder_expr(_DATE_PLACEHOLDER_START, style)
    end_ph   = _make_placeholder_expr(_DATE_PLACEHOLDER_END,   style)

    def _replacer(m: re.Match) -> str:
        bare = m.group(1)
        if bare == start_date:
            return start_ph
        if bare == end_date:
            return end_ph
        return m.group(0)   # leave unrelated date literals intact

    result = sql
    for pattern in _DATE_PATTERNS:
        result = pattern.sub(_replacer, result)

    success = _DATE_PLACEHOLDER_START in result and _DATE_PLACEHOLDER_END in result

    if not success:
        logger.warning(
            "parameterize_by_auto_extraction: placeholder substitution incomplete "
            "(extracted start=%s end=%s style=%s).",
            start_date, end_date, style,
        )
        return sql, False, "plain"

    logger.info(
        "parameterize_by_auto_extraction: SUCCESS — extracted time filter "
        "%s..%s from SQL (style=%s, time_col=%s).",
        start_date, end_date, style, time_col,
    )
    return result, True, style


def restore_sql_dates(
    sql_template: str,
    start_date: str,
    end_date: str,
    style: str = "plain",
) -> str:
    """
    Inverse of ``parameterize_sql_dates`` / ``parameterize_by_auto_extraction``.

    Replaces ``{start_date}`` and ``{end_date}`` placeholder tokens (in
    whichever wrapper form they were stored) with the actual date values.

    Args:
        sql_template: SQL with placeholder tokens.
        start_date:   Grain-adjusted start date in ``YYYY-MM-DD`` format.
        end_date:     Grain-adjusted end date in ``YYYY-MM-DD`` format.
        style:        Wrapper style stored alongside the template.

    Returns:
        Executable SQL with concrete date literals substituted in.
    """
    start_expr = _make_placeholder_expr(_DATE_PLACEHOLDER_START, style)
    end_expr   = _make_placeholder_expr(_DATE_PLACEHOLDER_END,   style)

    start_literal = _make_placeholder_expr(start_date, style)
    end_literal   = _make_placeholder_expr(end_date,   style)

    result = sql_template.replace(start_expr, start_literal)
    result = result.replace(end_expr,   end_literal)

    # Safety net: bare token replacement in case of style mismatch
    result = result.replace(_DATE_PLACEHOLDER_START, start_date)
    result = result.replace(_DATE_PLACEHOLDER_END,   end_date)

    return result
