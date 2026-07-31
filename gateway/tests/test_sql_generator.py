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


# ──────────────────────────────────────────────────────────────────────────────
# Filters on a column that is ALSO a group-by dimension.
#
# These used to be dropped as "redundant", which silently widened the answer:
# "how many subscribers churned in 2025 for country US" returned all 15
# countries. The predicate is now applied to the outer result instead, which
# keeps the L1 template cache usable AND actually narrows.
# ──────────────────────────────────────────────────────────────────────────────

class _StubTemplateCache:
    """Minimal SQLTemplateCache stand-in that records what gets stored."""

    def __init__(self, template_sql: str | None = None) -> None:
        self._template_sql = template_sql
        self.stored: list[tuple] = []

    def get(self, metrics, dimensions):
        if self._template_sql is None:
            return None
        return {
            "sql_template": self._template_sql,
            "has_time_filter": False,
            "date_style": "plain",
        }

    def set(self, metrics, dimensions, sql_template, has_placeholder, date_style="plain"):
        self.stored.append((metrics, dimensions, sql_template))


_COUNTRY_TEMPLATE = (
    "SELECT subscriber__country, SUM(__churned_subscribers) AS churned_subscribers\n"
    "FROM STREAMING_ANALYTICS.marts.dim_subscribers\n"
    "GROUP BY subscriber__country"
)


def _country_intent(filters: list[FilterClause]) -> QueryIntent:
    return QueryIntent(
        original_query="How many subscribers churned in 2025 for Country US",
        metrics=["churned_subscribers"],
        dimensions=["subscriber__country"],
        filters=filters,
    )


def test_narrowing_filter_on_grouped_dimension_reaches_the_sql(monkeypatch) -> None:
    """The regression: a country filter must survive, not be stripped."""
    monkeypatch.setattr(sql_generator, "build_dimension_prefix_map", lambda: {})
    cache = _StubTemplateCache(_COUNTRY_TEMPLATE)
    generator = SQLGenerator(_settings(), template_cache=cache)

    intent = _country_intent([
        FilterClause(column="subscriber__country", operator="eq", value="US")
    ])
    result = generator.generate(intent, _validation())

    assert "subscriber__country = 'US'" in result.compiled_sql
    # and it still came from the cached template, not a 30s MetricFlow compile
    assert "template_cache" in result.metricflow_query


def test_template_cache_is_still_used_for_a_filtered_group_by(monkeypatch) -> None:
    monkeypatch.setattr(sql_generator, "build_dimension_prefix_map", lambda: {})
    cache = _StubTemplateCache(_COUNTRY_TEMPLATE)
    generator = SQLGenerator(_settings(), template_cache=cache)

    result = generator.generate(
        _country_intent([FilterClause(column="subscriber__country", operator="eq", value="US")]),
        _validation(),
    )
    # The unfiltered template body is preserved inside the wrapper.
    assert "GROUP BY subscriber__country" in result.compiled_sql
    assert result.compiled_sql.strip().startswith("SELECT * FROM (")


def test_filtered_query_never_poisons_the_template_cache(monkeypatch) -> None:
    """A 'country = US' request must not overwrite the shared metric x country key."""
    monkeypatch.setattr(sql_generator, "build_dimension_prefix_map", lambda: {})
    cache = _StubTemplateCache(_COUNTRY_TEMPLATE)
    generator = SQLGenerator(_settings(), template_cache=cache)

    generator.generate(
        _country_intent([FilterClause(column="subscriber__country", operator="eq", value="US")]),
        _validation(),
    )
    for _metrics, _dims, stored_sql in cache.stored:
        assert "'US'" not in stored_sql, "filtered SQL was cached as a reusable template"


def test_all_values_in_filter_still_returns_every_group(monkeypatch) -> None:
    """The LLM's habit of enumerating every value must remain a no-op in practice."""
    monkeypatch.setattr(sql_generator, "build_dimension_prefix_map", lambda: {})
    cache = _StubTemplateCache(_COUNTRY_TEMPLATE)
    generator = SQLGenerator(_settings(), template_cache=cache)

    result = generator.generate(
        _country_intent([
            FilterClause(column="subscriber__country", operator="in", value=["US", "UK", "IN"])
        ]),
        _validation(),
    )
    assert "IN ('US', 'UK', 'IN')" in result.compiled_sql


def test_wrap_with_outer_predicates_escapes_quotes() -> None:
    sql = "SELECT country, x FROM t GROUP BY country"
    wrapped = sql_generator.wrap_with_outer_predicates(
        sql, [("country", FilterClause(column="country", operator="eq", value="O'Brien"))]
    )
    assert "'O''Brien'" in wrapped


def test_wrap_with_outer_predicates_rejects_unsafe_column() -> None:
    sql = "SELECT country FROM t"
    wrapped = sql_generator.wrap_with_outer_predicates(
        sql, [("country; DROP TABLE t", FilterClause(column="c", operator="eq", value="US"))]
    )
    assert wrapped == sql  # no predicate rendered, original returned untouched


def test_wrap_with_outer_predicates_is_noop_without_filters() -> None:
    sql = "SELECT 1"
    assert sql_generator.wrap_with_outer_predicates(sql, []) == sql


# ──────────────────────────────────────────────────────────────────────────────
# Warm in-process MetricFlow engine.
#
# The engine and the subprocess receive the identical argv list from
# format_mf_query(), so the two paths cannot produce different SQL. These cover
# the argv->request translation and the fall-back-on-failure contract.
# ──────────────────────────────────────────────────────────────────────────────

from core.metricflow_engine import WarmMetricFlowEngine


class _FakeWarmEngine:
    """Stands in for WarmMetricFlowEngine without loading dbt."""

    def __init__(self, sql: str | None = "SELECT 1 AS mrr", raises: bool = False) -> None:
        self._sql = sql
        self._raises = raises
        self.calls: list[list[str]] = []

    def explain_argv(self, mf_command: list[str]) -> str:
        self.calls.append(mf_command)
        if self._raises:
            raise RuntimeError("engine exploded")
        return self._sql


def test_parse_argv_translates_every_flag() -> None:
    argv = [
        "mf", "query",
        "--metrics", "mrr,churn_rate",
        "--group-by", "subscription__plan_type,subscriber__country",
        "--start-time", "2025-01-01",
        "--end-time", "2025-12-31",
        "--where", "{{ Dimension('subscriber__country') }} = 'US'",
        "--limit", "25",
        "--explain",
    ]
    kw = WarmMetricFlowEngine._parse_argv(argv)

    assert kw["metric_names"] == ["mrr", "churn_rate"]
    assert kw["group_by_names"] == ["subscription__plan_type", "subscriber__country"]
    assert kw["time_constraint_start"].year == 2025
    assert kw["time_constraint_end"].month == 12
    assert kw["where_constraints"] == ["{{ Dimension('subscriber__country') }} = 'US'"]
    assert kw["limit"] == 25


def test_parse_argv_bare_metric_has_no_group_by() -> None:
    kw = WarmMetricFlowEngine._parse_argv(["mf", "query", "--metrics", "mrr", "--explain"])
    assert kw["metric_names"] == ["mrr"]
    assert "group_by_names" not in kw
    assert "time_constraint_start" not in kw


def test_parse_argv_ignores_non_integer_limit() -> None:
    kw = WarmMetricFlowEngine._parse_argv(
        ["mf", "query", "--metrics", "mrr", "--limit", "lots", "--explain"]
    )
    assert "limit" not in kw


def test_warm_engine_is_used_instead_of_subprocess(monkeypatch) -> None:
    generator = SQLGenerator(_settings(), warm_engine=_FakeWarmEngine("SELECT 42 AS mrr"))

    def _boom(*_a, **_k):
        raise AssertionError("subprocess must not run when the warm engine works")

    monkeypatch.setattr(SQLGenerator, "_run_mf_subprocess", _boom)

    assert generator._compile_metricflow(["mf", "query", "--metrics", "mrr", "--explain"]) == (
        "SELECT 42 AS mrr"
    )


def test_warm_engine_failure_falls_back_to_subprocess(monkeypatch) -> None:
    """A broken engine must degrade to the slow path, never surface an error."""
    generator = SQLGenerator(_settings(), warm_engine=_FakeWarmEngine(raises=True))
    monkeypatch.setattr(
        SQLGenerator, "_run_mf_subprocess", lambda self, cmd: "SELECT 'from subprocess'"
    )

    assert generator._compile_metricflow(["mf", "query", "--metrics", "mrr", "--explain"]) == (
        "SELECT 'from subprocess'"
    )


def test_no_warm_engine_uses_subprocess(monkeypatch) -> None:
    generator = SQLGenerator(_settings())  # warm_engine defaults to None
    monkeypatch.setattr(
        SQLGenerator, "_run_mf_subprocess", lambda self, cmd: "SELECT 'from subprocess'"
    )

    assert generator._compile_metricflow(["mf", "query", "--metrics", "mrr", "--explain"]) == (
        "SELECT 'from subprocess'"
    )


def test_warm_engine_receives_the_same_argv_as_the_subprocess(monkeypatch) -> None:
    """Parity guard: one source of truth for how an intent becomes an mf query."""
    monkeypatch.setattr(
        sql_generator,
        "build_dimension_prefix_map",
        lambda: {"total_subscribers": {"plan_type": "subscriber__plan_type"}},
    )
    engine = _FakeWarmEngine()
    generator = SQLGenerator(_settings(), warm_engine=engine)
    intent = _intent(["subscriber__plan_type"])

    expected_argv = generator.format_mf_query(intent)
    generator._compile_metricflow(expected_argv)

    assert engine.calls == [expected_argv]


def test_try_build_returns_none_without_a_dbt_project(tmp_path) -> None:
    """Missing dbt project must disable the engine, not crash startup."""
    assert WarmMetricFlowEngine.try_build(str(tmp_path), str(tmp_path)) is None


# ──────────────────────────────────────────────────────────────────────────────


# ------------------------------------------------------------------------------
# MetricFlow --explain output parsing.
#
# The parser used to stop at the first blank line. MetricFlow puts one between
# the CTE list and the outer SELECT for multi-CTE queries, so `ltv` grouped by a
# joined dimension was truncated to its CTE -- 155 of 1882 chars, invalid SQL,
# silently cached, and shipped inside .sql_template_cache.json.
# ------------------------------------------------------------------------------

# Mirrors real `mf query --metrics ltv --group-by subscriber__country --explain`
# output: a CTE, a BLANK LINE, then the outer SELECT.
_LTV_STDOUT = """WITH sma_10005_cte AS (
  SELECT
    subscriber_id AS subscriber
    , country
  FROM STREAMING_ANALYTICS.marts.dim_subscribers sem_subscribers_src_10000
)

SELECT
  subscriber__country AS subscriber__country
  , CAST(total_revenue AS DOUBLE) / CAST(NULLIF(total_subscribers, 0) AS DOUBLE) AS ltv
FROM (
  SELECT
    COALESCE(subq_12.subscriber__country, subq_22.subscriber__country) AS subscriber__country
  FROM sma_10005_cte subq_12
) subq_30
"""


def test_extract_sql_keeps_everything_after_a_blank_line() -> None:
    """The regression: the outer SELECT must survive the blank line."""
    generator = SQLGenerator(_settings())
    sql = generator._extract_sql_from_mf_output(_LTV_STDOUT, "mf query ...")

    assert "sma_10005_cte" in sql
    assert "AS ltv" in sql, "outer SELECT was truncated"
    assert len(sql) > 400


def test_extract_sql_stops_at_cli_emoji_chrome() -> None:
    """Trailing CLI decoration must not be captured as SQL."""
    generator = SQLGenerator(_settings())
    chrome = "\nSuccess \U0001f984 - query completed after 1.20 seconds\n"
    sql = generator._extract_sql_from_mf_output(_LTV_STDOUT + chrome, "mf query ...")

    assert "Success" not in sql
    assert "AS ltv" in sql


def test_extract_sql_rejects_truncated_cte_only_output() -> None:
    """A CTE with no outer SELECT must raise, not be returned and cached."""
    generator = SQLGenerator(_settings())
    truncated = (
        "WITH sma_10005_cte AS (\n"
        "  SELECT subscriber_id AS subscriber, country\n"
        "  FROM STREAMING_ANALYTICS.marts.dim_subscribers\n"
        ")"
    )
    with pytest.raises(SQLGenerationError, match="no top-level SELECT"):
        generator._extract_sql_from_mf_output(truncated, "mf query ...")


def test_extract_sql_accepts_plain_select() -> None:
    generator = SQLGenerator(_settings())
    sql = generator._extract_sql_from_mf_output(
        "SELECT SUM(mrr_usd) AS mrr\nFROM STREAMING_ANALYTICS.marts.fct_mrr_monthly\n",
        "mf query ...",
    )
    assert sql.startswith("SELECT")


def test_has_top_level_select_distinguishes_truncation() -> None:
    assert sql_generator._has_top_level_select("SELECT 1")
    assert sql_generator._has_top_level_select("WITH c AS (SELECT 1) SELECT * FROM c")
    assert sql_generator._has_top_level_select("SELECT * FROM (SELECT 1) x")
    assert not sql_generator._has_top_level_select("WITH c AS (SELECT 1)")
    assert not sql_generator._has_top_level_select("WITH c AS (\n SELECT 1\n)\n")


# ──────────────────────────────────────────────────────────────────────────────
# Lazy construction of the warm engine.
#
# Building it costs ~15s and ~100 MB, so it must happen on the first compile
# rather than at startup — otherwise every TestClient lifespan boot pays it, and
# so does a deployment that never misses the template cache.
# ──────────────────────────────────────────────────────────────────────────────

def _settings_with_engine(**over):
    s = _settings()
    s.metricflow_in_process = True
    s.dbt_project_dir = "../dbt_streaming_analytics/streaming_analytics"
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_engine_is_not_built_when_config_disables_it(monkeypatch) -> None:
    built = []
    generator = SQLGenerator(_settings_with_engine(metricflow_in_process=False))
    monkeypatch.setattr(
        sql_generator.SQLGenerator, "_run_mf_subprocess", lambda self, cmd: "SELECT 1"
    )
    import core.metricflow_engine as me
    monkeypatch.setattr(
        me.WarmMetricFlowEngine, "try_build",
        classmethod(lambda cls, **kw: built.append(kw) or None),
    )

    generator._compile_metricflow(["mf", "query", "--metrics", "mrr", "--explain"])
    assert built == [], "try_build ran despite metricflow_in_process=False"


def test_engine_build_is_attempted_only_once(monkeypatch) -> None:
    """A deployment where the engine cannot build must not re-pay ~15s per miss."""
    calls = []
    import core.metricflow_engine as me
    monkeypatch.setattr(
        me.WarmMetricFlowEngine, "try_build",
        classmethod(lambda cls, **kw: calls.append(kw) or None),
    )
    monkeypatch.setattr(
        sql_generator.SQLGenerator, "_run_mf_subprocess", lambda self, cmd: "SELECT 1"
    )
    generator = SQLGenerator(_settings_with_engine())

    for _ in range(4):
        generator._compile_metricflow(["mf", "query", "--metrics", "mrr", "--explain"])

    assert len(calls) == 1, f"try_build ran {len(calls)} times, expected 1"


def test_lazy_engine_is_used_once_built(monkeypatch) -> None:
    fake = _FakeWarmEngine("SELECT 'lazy'")
    import core.metricflow_engine as me
    monkeypatch.setattr(
        me.WarmMetricFlowEngine, "try_build", classmethod(lambda cls, **kw: fake)
    )
    monkeypatch.setattr(
        sql_generator.SQLGenerator, "_run_mf_subprocess",
        lambda self, cmd: pytest.fail("subprocess ran despite a working lazy engine"),
    )
    generator = SQLGenerator(_settings_with_engine())

    argv = ["mf", "query", "--metrics", "mrr", "--explain"]
    assert generator._compile_metricflow(argv) == "SELECT 'lazy'"
    assert generator._compile_metricflow(argv) == "SELECT 'lazy'"
    assert len(fake.calls) == 2


def test_injected_engine_skips_the_lazy_build(monkeypatch) -> None:
    """An explicitly injected engine must be used as-is — no build attempt."""
    import core.metricflow_engine as me
    monkeypatch.setattr(
        me.WarmMetricFlowEngine, "try_build",
        classmethod(lambda cls, **kw: pytest.fail("try_build ran for an injected engine")),
    )
    generator = SQLGenerator(_settings_with_engine(), warm_engine=_FakeWarmEngine("SELECT 9"))

    assert generator._compile_metricflow(
        ["mf", "query", "--metrics", "mrr", "--explain"]
    ) == "SELECT 9"


def test_missing_dbt_project_dir_disables_the_engine(monkeypatch) -> None:
    monkeypatch.setattr(
        sql_generator.SQLGenerator, "_run_mf_subprocess", lambda self, cmd: "SELECT 'sub'"
    )
    generator = SQLGenerator(_settings_with_engine(dbt_project_dir=""))

    assert generator._compile_metricflow(
        ["mf", "query", "--metrics", "mrr", "--explain"]
    ) == "SELECT 'sub'"


def test_prewarm_is_disabled_under_pytest() -> None:
    """
    The suite must never pre-warm the engine.

    tests/test_query_endpoint.py boots the real lifespan per test, so leaving
    pre-warm on would start ~17 concurrent engine builds at ~27s and ~100 MB each.
    Tests never miss the template cache, so they never need the engine.
    """
    from config import Settings

    assert Settings().metricflow_prewarm is False


# ──────────────────────────────────────────────────────────────────────────────
# MetricFlow-first ordering.
#
# The semantic layer is the source of truth and compiling from it costs ~60-96 ms
# in production, so every query compiles. The template cache demotes to a
# fallback for when the warm engine is unavailable — which is what stops a stale
# or truncated committed template from ever being served on the happy path.
# ──────────────────────────────────────────────────────────────────────────────

_MRR_TEMPLATE = (
    "SELECT subscription__plan_type, SUM(mrr) AS mrr\n"
    "FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly\n"
    "GROUP BY subscription__plan_type"
)


def _mrr_plan_intent(filters=None):
    return QueryIntent(
        original_query="mrr by plan type",
        metrics=["mrr"],
        dimensions=["subscription__plan_type"],
        filters=filters or [],
    )


def test_metricflow_runs_even_when_the_template_cache_has_the_key(monkeypatch) -> None:
    """The regression guard for MetricFlow-first: a warm cache must not short-circuit."""
    monkeypatch.setattr(sql_generator, "build_dimension_prefix_map", lambda: {})
    cache = _StubTemplateCache(_MRR_TEMPLATE)
    engine = _FakeWarmEngine("SELECT 'from metricflow' AS mrr")
    generator = SQLGenerator(_settings(), template_cache=cache, warm_engine=engine)

    result = generator.generate(_mrr_plan_intent(), _validation())

    assert "from metricflow" in result.compiled_sql
    assert engine.calls, "warm engine was not consulted"
    assert "GROUP BY subscription__plan_type" not in result.compiled_sql


def test_template_cache_serves_when_no_warm_engine(monkeypatch) -> None:
    """Without an engine the cache must still answer — never a 30s subprocess."""
    monkeypatch.setattr(sql_generator, "build_dimension_prefix_map", lambda: {})
    monkeypatch.setattr(
        SQLGenerator, "_run_mf_subprocess",
        lambda self, cmd: pytest.fail("subprocess ran while a cached template existed"),
    )
    cache = _StubTemplateCache(_MRR_TEMPLATE)
    generator = SQLGenerator(_settings(), template_cache=cache)  # settings lack the flag

    result = generator.generate(_mrr_plan_intent(), _validation())

    assert "GROUP BY subscription__plan_type" in result.compiled_sql
    assert "template_cache" in result.metricflow_query


def test_engine_error_falls_through_to_the_cache(monkeypatch) -> None:
    monkeypatch.setattr(sql_generator, "build_dimension_prefix_map", lambda: {})
    monkeypatch.setattr(
        SQLGenerator, "_run_mf_subprocess",
        lambda self, cmd: pytest.fail("subprocess ran while a cached template existed"),
    )
    cache = _StubTemplateCache(_MRR_TEMPLATE)
    generator = SQLGenerator(
        _settings(), template_cache=cache, warm_engine=_FakeWarmEngine(raises=True)
    )

    result = generator.generate(_mrr_plan_intent(), _validation())

    assert "GROUP BY subscription__plan_type" in result.compiled_sql


def test_filtered_query_reaches_metricflow_not_the_fallback_builder(monkeypatch) -> None:
    """
    With an engine available, filters compile via --where.

    The fallback builder is a hand-written re-implementation of each metric and is
    where the churn_date/signup_date divergence lives, so it must not be the
    default route for filtered queries any more.
    """
    monkeypatch.setattr(sql_generator, "build_dimension_prefix_map", lambda: {})
    monkeypatch.setattr(
        SQLGenerator, "_build_fallback_sql",
        lambda self, intent: pytest.fail("fallback builder ran despite a warm engine"),
    )
    engine = _FakeWarmEngine("SELECT 'mf filtered' AS mrr")
    generator = SQLGenerator(_settings(), warm_engine=engine)

    intent = QueryIntent(
        original_query="mrr for premium",
        metrics=["mrr"],
        dimensions=[],
        filters=[FilterClause(column="subscription__plan_type", operator="eq", value="premium")],
    )
    result = generator.generate(intent, _validation())

    assert "mf filtered" in result.compiled_sql
    assert "--where" in " ".join(engine.calls[0])
