from __future__ import annotations

from types import SimpleNamespace

import core.sql_generator as sql_generator
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
