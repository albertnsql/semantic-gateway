from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.sql_generator as sql_generator
from core.exceptions import SQLGenerationError
from core.intent_extractor import FilterClause, QueryIntent, TimeRange
from core.semantic_validator import ValidationResult
from core.sql_generator import SQLGenerator


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        snowflake_account="account",
        snowflake_user="user",
        snowflake_password="password",
        snowflake_database="STREAMING_ANALYTICS",
        snowflake_warehouse="warehouse",
        snowflake_role="role",
        snowflake_schema="marts",
    )


def _intent(dimensions: list[str]) -> QueryIntent:
    return QueryIntent(
        original_query="Show me total subscribers by plan type",
        metrics=["total_subscribers"],
        dimensions=dimensions,
    )


def _validation() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        validation_passed=["metrics_certified", "dimensions_certified"],
        violations=[],
        safe_to_execute=True,
        suggested_fix=None,
    )


def test_format_mf_query_maps_prefixed_dimension_to_metricflow_name(monkeypatch) -> None:
    monkeypatch.setattr(
        sql_generator,
        "build_dimension_prefix_map",
        lambda: {"total_subscribers": {"plan_type": "subscriber__plan_type"}},
    )

    generator = SQLGenerator(_settings())

    assert (
        generator.format_mf_query(_intent(["subscriber__plan_type"]))
        == ["mf", "query", "--metrics", "total_subscribers", "--group-by", "subscriber__plan_type", "--explain"]
    )


def test_fallback_sql_strips_metricflow_prefix_from_physical_column() -> None:
    generator = SQLGenerator(_settings())

    sql = generator._build_fallback_sql(_intent(["subscriber__plan_type"]))

    assert "subscriber__plan_type" not in sql
    assert "plan_type" in sql


def test_generate_sets_utf8_env_and_fallback_strips_prefixed_dimension(monkeypatch) -> None:
    captured_env = {}

    def fake_run(*args, **kwargs):
        captured_env.update(kwargs["env"])
        return SimpleNamespace(returncode=1, stderr="metricflow cli failed", stdout="")

    monkeypatch.setattr(sql_generator.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sql_generator,
        "build_dimension_prefix_map",
        lambda: {"total_subscribers": {"plan_type": "subscriber__plan_type"}},
    )
    monkeypatch.setattr(
        SQLGenerator,
        "_review_sql",
        lambda self, sql: {"approved": True, "sql": sql},
    )

    generator = SQLGenerator(_settings())
    query = generator.generate(_intent(["subscriber__plan_type"]), _validation())

    assert captured_env["PYTHONUTF8"] == "1"
    assert captured_env["PYTHONIOENCODING"] == "utf-8"
    assert captured_env["NO_COLOR"] == "1"
    assert "subscriber__plan_type" not in query.compiled_sql
    assert "plan_type" in query.compiled_sql


# ── Option B: filtered queries served by the in-process fallback builder ──────────


def _mrr_intent(dimensions: list[str], filters: list[FilterClause]) -> QueryIntent:
    return QueryIntent(
        original_query="mrr filtered query",
        metrics=["mrr"],
        dimensions=dimensions,
        filters=filters,
        time_range=TimeRange(
            start_date="2026-04-01", end_date="2026-07-01", relative="last_3_months"
        ),
    )


def test_filtered_query_uses_fallback_builder_and_skips_metricflow(monkeypatch) -> None:
    """A filter must route to the in-process builder, never the ~30 s MetricFlow subprocess."""
    ran = {"metricflow": False}

    def fake_run(*args, **kwargs):
        ran["metricflow"] = True
        return SimpleNamespace(returncode=1, stderr="", stdout="")

    monkeypatch.setattr(sql_generator.subprocess, "run", fake_run)
    monkeypatch.setattr(sql_generator, "build_dimension_prefix_map", lambda: {})

    generator = SQLGenerator(_settings())
    query = generator.generate(
        _mrr_intent(
            ["subscription__plan_type"],
            [FilterClause(column="subscriber__country", operator="eq", value="US")],
        ),
        _validation(),
    )

    assert ran["metricflow"] is False, "filtered query must NOT invoke MetricFlow"
    assert query.sql_review.get("source") == "fallback_builder"
    assert "country = 'US'" in query.compiled_sql


def test_fallback_sql_joins_dim_subscribers_for_cross_table_filter() -> None:
    """country lives on dim_subscribers, not fct_mrr_monthly → the builder must join it."""
    generator = SQLGenerator(_settings())
    sql = generator._build_fallback_sql(
        _mrr_intent([], [FilterClause(column="subscriber__country", operator="eq", value="US")])
    )

    assert "LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers" in sql
    assert "sub.country" in sql
    assert "country = 'US'" in sql


def test_fallback_sql_no_join_for_same_table_filter() -> None:
    """plan_type is on fct_mrr_monthly → no join needed, flat query."""
    generator = SQLGenerator(_settings())
    sql = generator._build_fallback_sql(
        _mrr_intent([], [FilterClause(column="subscription__plan_type", operator="eq", value="premium")])
    )

    assert "dim_subscribers" not in sql
    assert "plan_type = 'premium'" in sql


def test_fallback_sql_escapes_single_quotes_in_filter_value() -> None:
    """Filter values must have single quotes doubled to neutralise injection."""
    generator = SQLGenerator(_settings())
    sql = generator._build_fallback_sql(
        _mrr_intent([], [FilterClause(column="subscription__plan_type", operator="eq", value="a' OR '1'='1")])
    )

    assert "a'' OR ''1''=''1" in sql


# ── Metric mapping coverage & fail-loud guard ─────────────────────────────────


def _metric_intent(metric: str, dimensions: list[str] | None = None) -> QueryIntent:
    return QueryIntent(
        original_query=f"{metric} query",
        metrics=[metric],
        dimensions=dimensions or [],
        filters=[],
        time_range=TimeRange(
            start_date="2026-04-01", end_date="2026-07-01", relative="last_3_months"
        ),
    )


# metric name → the aggregation expected in the fallback SQL (mirrors sem_stream_sessions).
_STREAMING_METRICS = {
    "avg_watch_time": "AVG(duration_minutes)",
    "total_watch_time": "SUM(duration_minutes)",
    "total_sessions": "COUNT(session_id)",
    "avg_buffering_events": "AVG(buffering_events)",
    "total_buffering_events": "SUM(buffering_events)",
}


@pytest.mark.parametrize("metric,expr", list(_STREAMING_METRICS.items()))
def test_fallback_maps_streaming_metrics_to_sessions_table(metric, expr) -> None:
    """Streaming metrics must hit fct_stream_sessions with session_start — never the MRR table."""
    generator = SQLGenerator(_settings())
    sql = generator._build_fallback_sql(_metric_intent(metric, ["session__device_type"]))

    assert "STREAMING_ANALYTICS.marts.fct_stream_sessions" in sql
    assert "fct_mrr_monthly" not in sql
    assert expr in sql
    assert "session_start BETWEEN" in sql


def test_fallback_rejects_net_mrr_growth() -> None:
    """net_mrr_growth (offset-window) must fail loudly, pointing to the dashboard widget."""
    generator = SQLGenerator(_settings())
    with pytest.raises(SQLGenerationError) as exc:
        generator._build_fallback_sql(_metric_intent("net_mrr_growth"))

    assert "net_mrr_growth" in str(exc.value)
    assert "dashboard" in str(exc.value).lower()


def test_fallback_rejects_unmapped_metric() -> None:
    """An unmapped metric must raise a clear error, not silently default to fct_mrr_monthly."""
    generator = SQLGenerator(_settings())
    with pytest.raises(SQLGenerationError):
        generator._build_fallback_sql(_metric_intent("some_unmapped_metric"))


def test_extract_sql_captures_leading_with_cte() -> None:
    """MetricFlow CTE output must be captured starting at WITH, not the inner SELECT —
    dropping the `WITH cte AS (` prefix leaves a dangling ')' (Snowflake syntax error)."""
    generator = SQLGenerator(_settings())
    stdout = (
        "Success - query completed\n"
        "SQL:\n"
        "WITH cte_0 AS (\n"
        "  SELECT subscriber_id, amount_usd\n"
        "  FROM STREAMING_ANALYTICS.marts.fct_payments\n"
        ")\n"
        "SELECT plan_type, SUM(amount_usd) AS ltv\n"
        "FROM cte_0\n"
        "GROUP BY plan_type\n"
    )
    sql = generator._extract_sql_from_mf_output(stdout, "mf query ...")

    assert sql.upper().startswith("WITH")
    assert "cte_0" in sql
    assert sql.count("(") == sql.count(")")  # balanced → prefix not dropped


def test_extract_sql_captures_leading_select() -> None:
    """Plain SELECT output (no CTE) still extracts cleanly."""
    generator = SQLGenerator(_settings())
    stdout = (
        "SQL:\n"
        "SELECT plan_type, SUM(mrr_usd) AS mrr\n"
        "FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly\n"
        "GROUP BY plan_type\n"
    )
    sql = generator._extract_sql_from_mf_output(stdout, "mf query ...")

    assert sql.upper().startswith("SELECT")
    assert "fct_mrr_monthly" in sql


def test_fallback_total_subscribers_counts_active_on_mrr() -> None:
    """total_subscribers must count distinct ACTIVE subscribers on fct_mrr_monthly by period_month,
    matching the dashboard Active Subscribers KPI — not signups on dim_subscribers."""
    generator = SQLGenerator(_settings())
    sql = generator._build_fallback_sql(_metric_intent("total_subscribers"))

    assert "STREAMING_ANALYTICS.marts.fct_mrr_monthly" in sql
    assert "COUNT(DISTINCT CASE WHEN is_active = TRUE THEN subscriber_id END)" in sql
    assert "period_month BETWEEN" in sql
    assert "signup_date" not in sql
    assert "dim_subscribers" not in sql  # unfiltered → no subscriber join
