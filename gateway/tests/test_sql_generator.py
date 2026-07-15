from __future__ import annotations

from types import SimpleNamespace

import core.sql_generator as sql_generator
from core.intent_extractor import QueryIntent
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
