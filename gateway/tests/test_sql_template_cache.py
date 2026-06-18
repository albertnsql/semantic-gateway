"""
tests/test_sql_template_cache.py — Unit tests for core/sql_template_cache.py

Covers:
  - parameterize_sql_dates detects and replaces all MetricFlow date styles
    (CAST, ::DATE, DATE(), plain quoted literal)
  - parameterize_sql_dates leaves unrelated date literals intact
  - parameterize_sql_dates fails safely when dates are ambiguous (start == end)
  - parameterize_sql_dates fails safely when no dates found in SQL
  - restore_sql_dates is the exact inverse for every style
  - round-trip: parameterize then restore yields the original executable SQL
  - SQLTemplateCache.get / set / invalidate / clear / stats / LRU eviction
  - Cache hit returns correct date_style for style-aware restoration
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from core.sql_template_cache import (
    SQLTemplateCache,
    parameterize_sql_dates,
    restore_sql_dates,
)

# ---------------------------------------------------------------------------
# Sample MetricFlow SQL fixtures for each date style
# ---------------------------------------------------------------------------

START = "2026-03-18"
END = "2026-06-18"

# MetricFlow / dbt-snowflake adapter date patterns
SQL_CAST = (
    "SELECT plan_type, SUM(mrr_usd) AS mrr\n"
    "FROM fct_mrr_monthly\n"
    "WHERE period_month >= CAST('2026-03-18' AS DATE)\n"
    "  AND period_month <  CAST('2026-06-18' AS DATE)\n"
    "GROUP BY 1"
)

SQL_COLCAST = (
    "SELECT plan_type, SUM(mrr_usd) AS mrr\n"
    "FROM fct_mrr_monthly\n"
    "WHERE period_month >= '2026-03-18'::DATE\n"
    "  AND period_month <  '2026-06-18'::DATE\n"
    "GROUP BY 1"
)

SQL_DATEFN = (
    "SELECT plan_type, SUM(mrr_usd) AS mrr\n"
    "FROM fct_mrr_monthly\n"
    "WHERE period_month >= DATE('2026-03-18')\n"
    "  AND period_month <  DATE('2026-06-18')\n"
    "GROUP BY 1"
)

SQL_PLAIN = (
    "SELECT plan_type, SUM(mrr_usd) AS mrr\n"
    "FROM fct_mrr_monthly\n"
    "WHERE period_month BETWEEN '2026-03-18' AND '2026-06-18'\n"
    "GROUP BY 1"
)

# A more realistic MetricFlow output with a mix of aggregations + subquery
SQL_REALISTIC = (
    "SELECT\n"
    "  subq_3.plan_type AS subscription__plan_type,\n"
    "  SUM(subq_3.mrr_usd) AS mrr\n"
    "FROM (\n"
    "  SELECT\n"
    "    sub.plan_type,\n"
    "    mrr.mrr_usd\n"
    "  FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly AS mrr\n"
    "  LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers AS sub\n"
    "    ON mrr.subscriber_id = sub.subscriber_id\n"
    "  WHERE\n"
    "    mrr.period_month >= CAST('2026-03-18' AS DATE)\n"
    "    AND mrr.period_month < CAST('2026-06-18' AS DATE)\n"
    "    AND sub.is_active = TRUE\n"
    ") AS subq_3\n"
    "GROUP BY 1\n"
    "ORDER BY 1"
)

# SQL that has an unrelated date literal that must NOT be replaced
SQL_UNRELATED_DATE = (
    "SELECT plan_type, SUM(mrr_usd) AS mrr\n"
    "FROM fct_mrr_monthly\n"
    "WHERE period_month >= '2026-03-18'::DATE\n"
    "  AND period_month <  '2026-06-18'::DATE\n"
    "  AND signup_date > '2020-01-01'::DATE\n"   # unrelated — must survive
    "GROUP BY 1"
)


# ─────────────────────────────────────────────── parameterize_sql_dates


class TestParameterizeSqlDates:

    def test_cast_style_detected_and_replaced(self):
        sql, ok, style = parameterize_sql_dates(SQL_CAST, START, END)
        assert ok is True
        assert style == "cast"
        assert "{start_date}" in sql
        assert "{end_date}" in sql
        assert START not in sql
        assert END not in sql

    def test_colcast_style_detected_and_replaced(self):
        sql, ok, style = parameterize_sql_dates(SQL_COLCAST, START, END)
        assert ok is True
        assert style == "colcast"
        assert "{start_date}" in sql
        assert "{end_date}" in sql
        assert START not in sql
        assert END not in sql

    def test_datefn_style_detected_and_replaced(self):
        sql, ok, style = parameterize_sql_dates(SQL_DATEFN, START, END)
        assert ok is True
        assert style == "datefn"
        assert "{start_date}" in sql
        assert "{end_date}" in sql

    def test_plain_quoted_style_detected_and_replaced(self):
        sql, ok, style = parameterize_sql_dates(SQL_PLAIN, START, END)
        assert ok is True
        assert style == "plain"
        assert "{start_date}" in sql
        assert "{end_date}" in sql

    def test_realistic_metricflow_sql_cast_style(self):
        sql, ok, style = parameterize_sql_dates(SQL_REALISTIC, START, END)
        assert ok is True
        assert style == "cast"
        assert "{start_date}" in sql
        assert "{end_date}" in sql
        # The is_active filter must survive untouched
        assert "is_active = TRUE" in sql

    def test_unrelated_date_literal_is_preserved(self):
        """Dates not equal to start_date or end_date must not be replaced."""
        sql, ok, style = parameterize_sql_dates(SQL_UNRELATED_DATE, START, END)
        assert ok is True
        # The unrelated signup_date cutoff must still be in the SQL
        assert "2020-01-01" in sql

    def test_ambiguous_dates_returns_false(self):
        """When start == end we cannot distinguish literals — must fail safely."""
        same_date = "2026-06-18"
        sql, ok, style = parameterize_sql_dates(SQL_PLAIN, same_date, same_date)
        assert ok is False
        # Original SQL must be returned unchanged
        assert sql == SQL_PLAIN

    def test_dates_not_in_sql_returns_false(self):
        """If neither date appears in the SQL, return original + False."""
        sql = "SELECT 1 FROM fct_mrr_monthly"
        result, ok, style = parameterize_sql_dates(sql, START, END)
        assert ok is False
        assert result == sql

    def test_only_start_date_present_returns_false(self):
        """Partial presence (only one bound found) is treated as non-parameterizable."""
        sql = "SELECT * FROM t WHERE d >= '2026-03-18'::DATE"
        result, ok, _ = parameterize_sql_dates(sql, START, END)
        assert ok is False
        assert result == sql


# ─────────────────────────────────────────────── restore_sql_dates


class TestRestoreSqlDates:

    @pytest.mark.parametrize("sql,style", [
        (SQL_CAST,    "cast"),
        (SQL_COLCAST, "colcast"),
        (SQL_DATEFN,  "datefn"),
        (SQL_PLAIN,   "plain"),
    ])
    def test_round_trip_restores_original_sql(self, sql, style):
        """parameterize then restore must yield back the exact original SQL."""
        new_start = "2025-12-18"
        new_end   = "2026-06-18"

        parameterized, ok, detected_style = parameterize_sql_dates(sql, START, END)
        assert ok is True, f"Parameterization failed for style={style}"

        restored = restore_sql_dates(parameterized, new_start, new_end, style=detected_style)

        # The restored SQL must contain the *new* dates, not the originals
        assert new_start in restored or f"'{new_start}'" in restored or new_start in restored
        assert new_end   in restored or f"'{new_end}'"   in restored or new_end   in restored
        # The placeholder tokens must be gone
        assert "{start_date}" not in restored
        assert "{end_date}" not in restored

    def test_restore_cast_style_produces_cast_expr(self):
        parameterized, ok, style = parameterize_sql_dates(SQL_CAST, START, END)
        assert ok is True
        restored = restore_sql_dates(parameterized, "2025-12-18", "2026-06-18", style=style)
        assert "CAST('2025-12-18' AS DATE)" in restored
        assert "CAST('2026-06-18' AS DATE)" in restored

    def test_restore_colcast_style_produces_colcast_expr(self):
        parameterized, ok, style = parameterize_sql_dates(SQL_COLCAST, START, END)
        assert ok is True
        restored = restore_sql_dates(parameterized, "2025-12-18", "2026-06-18", style=style)
        assert "'2025-12-18'::DATE" in restored
        assert "'2026-06-18'::DATE" in restored

    def test_restore_plain_style_produces_quoted_literal(self):
        parameterized, ok, style = parameterize_sql_dates(SQL_PLAIN, START, END)
        assert ok is True
        restored = restore_sql_dates(parameterized, "2025-12-18", "2026-06-18", style=style)
        assert "'2025-12-18'" in restored
        assert "'2026-06-18'" in restored

    def test_restore_with_style_mismatch_falls_back_safely(self):
        """Safety net: bare token replacement works even if style is wrong."""
        # Manually craft a template with bare tokens (no wrappers)
        template = "WHERE d >= {start_date} AND d < {end_date}"
        restored = restore_sql_dates(template, "2025-01-01", "2026-01-01", style="plain")
        assert "2025-01-01" in restored
        assert "2026-01-01" in restored
        assert "{start_date}" not in restored


# ─────────────────────────────────────────────── SQLTemplateCache


class TestSQLTemplateCache:

    # ── basic get / set

    def test_miss_on_empty_cache(self):
        cache = SQLTemplateCache()
        assert cache.get(["mrr"], ["plan_type"]) is None

    def test_set_then_get_returns_template(self):
        cache = SQLTemplateCache()
        cache.set(["mrr"], ["plan_type"], "SELECT 1", has_time_filter=True, date_style="cast")
        entry = cache.get(["mrr"], ["plan_type"])
        assert entry is not None
        assert entry["sql_template"] == "SELECT 1"
        assert entry["has_time_filter"] is True
        assert entry["date_style"] == "cast"

    def test_different_metrics_different_keys(self):
        cache = SQLTemplateCache()
        cache.set(["mrr"], ["plan_type"], "SQL_A", has_time_filter=True, date_style="plain")
        assert cache.get(["ltv"], ["plan_type"]) is None

    def test_dimension_order_independent_key(self):
        """Sorting ensures dim order doesn't produce separate cache slots."""
        cache = SQLTemplateCache()
        cache.set(["mrr"], ["plan_type", "country"], "SQL", has_time_filter=True, date_style="plain")
        # reversed dim order must still hit the same slot
        assert cache.get(["mrr"], ["country", "plan_type"]) is not None

    def test_metric_order_independent_key(self):
        cache = SQLTemplateCache()
        cache.set(["mrr", "expansion_mrr"], ["plan_type"], "SQL", has_time_filter=True, date_style="plain")
        assert cache.get(["expansion_mrr", "mrr"], ["plan_type"]) is not None

    # ── TTL expiry

    def test_expired_entry_returns_none(self):
        cache = SQLTemplateCache(ttl_seconds=60)
        cache.set(["mrr"], ["plan_type"], "SELECT 1", has_time_filter=True, date_style="plain")
        with patch("core.sql_template_cache.time") as mock_time:
            mock_time.time.return_value = time.time() + 99999
            result = cache.get(["mrr"], ["plan_type"])
        assert result is None

    def test_expired_entry_is_removed(self):
        cache = SQLTemplateCache(ttl_seconds=60)
        cache.set(["mrr"], ["plan_type"], "SELECT 1", has_time_filter=True, date_style="plain")
        with patch("core.sql_template_cache.time") as mock_time:
            mock_time.time.return_value = time.time() + 99999
            cache.get(["mrr"], ["plan_type"])
        assert cache.stats()["total_entries"] == 0

    # ── LRU eviction

    def test_lru_eviction_removes_oldest(self):
        cache = SQLTemplateCache(maxsize=3)
        for i in range(4):
            cache.set([f"metric_{i}"], ["dim"], f"SQL_{i}", has_time_filter=False, date_style="plain")
        assert cache.get(["metric_0"], ["dim"]) is None   # evicted
        assert cache.get(["metric_3"], ["dim"]) is not None

    def test_total_entries_never_exceeds_maxsize(self):
        cache = SQLTemplateCache(maxsize=5)
        for i in range(20):
            cache.set([f"m{i}"], ["d"], "SQL", has_time_filter=False, date_style="plain")
        assert cache.stats()["total_entries"] <= 5

    def test_hit_refreshes_lru_position(self):
        cache = SQLTemplateCache(maxsize=3)
        cache.set(["a"], ["d"], "SQL", has_time_filter=False, date_style="plain")
        cache.set(["b"], ["d"], "SQL", has_time_filter=False, date_style="plain")
        cache.set(["c"], ["d"], "SQL", has_time_filter=False, date_style="plain")
        cache.get(["a"], ["d"])          # touch 'a' so it is no longer the oldest
        cache.set(["z"], ["d"], "SQL", has_time_filter=False, date_style="plain")  # evicts 'b'
        assert cache.get(["a"], ["d"]) is not None, "'a' should survive (was refreshed)"
        assert cache.get(["b"], ["d"]) is None,     "'b' should have been evicted"

    # ── invalidate / clear

    def test_invalidate_removes_specific_entry(self):
        cache = SQLTemplateCache()
        cache.set(["mrr"], ["plan_type"], "SQL_A", has_time_filter=True, date_style="plain")
        cache.set(["ltv"], ["plan_type"], "SQL_B", has_time_filter=True, date_style="plain")
        cache.invalidate(["mrr"], ["plan_type"])
        assert cache.get(["mrr"], ["plan_type"]) is None
        assert cache.get(["ltv"], ["plan_type"]) is not None

    def test_invalidate_nonexistent_is_safe(self):
        cache = SQLTemplateCache()
        cache.invalidate(["nonexistent"], ["dim"])  # must not raise

    def test_clear_empties_cache(self):
        cache = SQLTemplateCache()
        for i in range(5):
            cache.set([f"m{i}"], ["d"], "SQL", has_time_filter=False, date_style="plain")
        cache.clear()
        assert cache.stats()["total_entries"] == 0

    # ── stats

    def test_stats_empty_cache(self):
        cache = SQLTemplateCache(ttl_seconds=3600, maxsize=100)
        s = cache.stats()
        assert s["total_entries"] == 0
        assert s["active_entries"] == 0
        assert s["expired_entries"] == 0
        assert s["ttl_seconds"] == 3600
        assert s["maxsize"] == 100

    def test_stats_counts_active_vs_expired(self):
        cache = SQLTemplateCache(ttl_seconds=3600, maxsize=10)
        cache.set(["mrr"], ["d"], "SQL", has_time_filter=True, date_style="plain")
        cache.set(["ltv"], ["d"], "SQL", has_time_filter=True, date_style="plain")
        # Back-date one entry so it appears expired
        key = SQLTemplateCache.make_key(["mrr"], ["d"])
        cache._store[key]["expires_at"] = time.time() - 1

        s = cache.stats()
        assert s["total_entries"] == 2
        assert s["active_entries"] == 1
        assert s["expired_entries"] == 1

    # ── date_style round-trip through the cache

    def test_date_style_stored_and_retrieved_correctly(self):
        """
        End-to-end: parameterize real MetricFlow SQL, store in cache,
        retrieve and restore — resulting SQL must contain concrete dates
        in the original cast style.
        """
        cache = SQLTemplateCache()

        template, ok, style = parameterize_sql_dates(SQL_REALISTIC, START, END)
        assert ok is True
        assert style == "cast"

        cache.set(["mrr"], ["plan_type"], template, has_time_filter=ok, date_style=style)

        entry = cache.get(["mrr"], ["plan_type"])
        assert entry is not None
        assert entry["has_time_filter"] is True
        assert entry["date_style"] == "cast"

        restored = restore_sql_dates(
            entry["sql_template"],
            "2025-12-18",
            "2026-06-18",
            style=entry["date_style"],
        )
        assert "CAST('2025-12-18' AS DATE)" in restored
        assert "CAST('2026-06-18' AS DATE)" in restored
        assert "{start_date}" not in restored
        assert "{end_date}" not in restored
        # The non-date parts must be untouched
        assert "is_active = TRUE" in restored
        assert "SUM(subq_3.mrr_usd) AS mrr" in restored
