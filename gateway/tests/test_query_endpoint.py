"""
tests/test_query_endpoint.py — Integration tests for POST /api/v1/query
and GET /api/v1/health endpoints.

All core services are mocked so no real OpenAI, Snowflake, or MetricFlow
calls are made during testing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.exceptions import SnowflakeConnectionError
from core.intent_extractor import FilterClause, QueryIntent, TimeRange
from core.lineage_resolver import LineageTrace, TransformationStep
from core.semantic_validator import ValidationResult
from core.sql_generator import GeneratedQuery
from models.responses import (
    GatewayResponse,
    GovernanceBlock,
    QueryBlock,
    RejectionBlock,
    ResultBlock,
    ValidationBlock,
    ViolationDetail,
)
from models.semantic import MetricDefinition
from api.routes.query import _generate_narrative, _raw_query_cache_key


# ──────────────────────────────────────────────── Helpers

def _make_metric_def(name: str = "mrr") -> MetricDefinition:
    return MetricDefinition(
        name=name,
        label="Monthly Recurring Revenue",
        description="Total MRR across active subscriptions.",
        metric_type="simple",
        source_model="fct_mrr_monthly",
        grain="subscription+month",
        grain_columns=["subscription_id", "period_month"],
        certified_dimensions=["plan_type", "billing_cycle", "mrr_type", "period_month"],
        time_dimension="period_month",
        allowed_joins=["expansion_mrr", "churn_rate"],
        fanout_risk_models=["fct_stream_sessions"],
        measure_column="mrr_usd",
        lineage=["stg_subscriptions", "int_subscription_periods"],
        raw_yaml={},
    )


def _make_intent(
    metrics: list[str] = None,
    dimensions: list[str] = None,
    time_range: TimeRange | None = None,
) -> QueryIntent:
    return QueryIntent(
        original_query="What is the MRR by plan type?",
        metrics=metrics or ["mrr"],
        dimensions=dimensions or ["plan_type"],
        filters=[],
        time_range=time_range or TimeRange(
            start_date="2024-02-27", end_date="2024-05-27", relative="last_3_months"
        ),
    )


def _make_validation(passed: bool = True) -> ValidationResult:
    if passed:
        return ValidationResult(
            is_valid=True,
            validation_passed=["metrics_certified", "dimensions_certified",
                               "grain_compatibility", "fanout_risk",
                               "time_dimension", "filter_safety"],
            violations=[],
            safe_to_execute=True,
            suggested_fix=None,
        )
    else:
        return ValidationResult(
            is_valid=False,
            validation_passed=["metrics_certified"],
            violations=[
                ViolationDetail(
                    rule="dimensions_certified",
                    severity="ERROR",
                    message="Dimension 'unknown_dim' is not certified.",
                    affected_elements=["unknown_dim"],
                )
            ],
            safe_to_execute=False,
            suggested_fix="Use a certified dimension.",
        )


def _make_generated_query() -> GeneratedQuery:
    return GeneratedQuery(
        metricflow_query="mf query --metrics mrr --group-by plan_type --start-time 2024-02-27 --end-time 2024-05-27 --explain",
        compiled_sql="SELECT plan_type, SUM(mrr_usd) AS mrr FROM marts.fct_mrr_monthly GROUP BY 1",
        metrics=["mrr"],
        dimensions=["plan_type"],
        time_range=None,
        grain="subscription+month",
    )


def _make_lineage() -> LineageTrace:
    return LineageTrace(
        metric_name="mrr",
        source_model="fct_mrr_monthly",
        upstream_models=["int_subscription_periods", "stg_subscriptions"],
        source_tables=["raw.subscriptions"],
        transformation_steps=[
            TransformationStep(model_name="stg_subscriptions", layer="staging", description="", columns_used=[]),
            TransformationStep(model_name="int_subscription_periods", layer="intermediate", description="", columns_used=[]),
            TransformationStep(model_name="fct_mrr_monthly", layer="marts", description="", columns_used=[]),
        ],
    )


def _make_success_response(request_id: str = "test-id") -> GatewayResponse:
    intent = _make_intent()
    return GatewayResponse(
        request_id=request_id,
        timestamp=datetime.now(timezone.utc),
        status="success",
        query=QueryBlock(
            original=intent.original_query,
            interpreted_metrics=intent.metrics,
            interpreted_dimensions=intent.dimensions,
            time_range=None,
        ),
        validation=ValidationBlock(passed=True, checks_run=[], violations=[]),
        governance=GovernanceBlock(
            certified_definition="MRR is...",
            grain="subscription+month",
            grain_columns=["subscription_id"],
            lineage_path=["stg_subscriptions", "fct_mrr_monthly"],
            source_model="fct_mrr_monthly",
        ),
        result=ResultBlock(
            row_count=2,
            data=[{"plan_type": "basic", "mrr": 10000}, {"plan_type": "premium", "mrr": 25000}],
            generated_sql="SELECT ...",
            metricflow_query="mf query ...",
        ),
        rejection=RejectionBlock(),
    )


def _make_rejection_response(request_id: str = "test-id") -> GatewayResponse:
    intent = _make_intent()
    return GatewayResponse(
        request_id=request_id,
        timestamp=datetime.now(timezone.utc),
        status="rejected",
        query=QueryBlock(
            original=intent.original_query,
            interpreted_metrics=["mrr"],
            interpreted_dimensions=["unknown_dim"],
            time_range=None,
        ),
        validation=ValidationBlock(
            passed=False,
            checks_run=["metrics_certified"],
            violations=[
                ViolationDetail(
                    rule="dimensions_certified",
                    severity="ERROR",
                    message="Dimension 'unknown_dim' is not certified.",
                    affected_elements=["unknown_dim"],
                )
            ],
        ),
        governance=GovernanceBlock(),
        result=ResultBlock(),
        rejection=RejectionBlock(
            reason="Dimension 'unknown_dim' is not certified.",
            violated_rules=["dimensions_certified"],
            suggested_fix="Use a certified dimension.",
            safe_alternatives=["expansion_mrr"],
        ),
    )


# ──────────────────────────────────────────────── App fixture

@pytest.fixture
def client():
    """
    Create a FastAPI TestClient with all core services mocked on app.state.

    IMPORTANT: Mocks are injected AFTER the TestClient context starts because
    FastAPI's lifespan initialises app.state on startup and would overwrite any
    mocks set before the 'with TestClient(app)' block.
    """
    from main import app

    metric = _make_metric_def()

    # Build mock services
    mock_registry = MagicMock()
    mock_registry.list_metrics.return_value = [metric]
    mock_registry.get_metric.return_value = metric
    mock_registry.is_certified_metric.return_value = True
    mock_registry.is_certified_dimension.return_value = True
    mock_registry.get_dimensions_for_metric.return_value = metric.certified_dimensions
    mock_registry.get_all_dimension_map.return_value = {"mrr": metric.certified_dimensions}
    mock_registry.would_cause_fanout.return_value = False
    mock_registry.count_semantic_models.return_value = 5
    # Stub time-grain lookup so Stage 1.5 grain/clarification check never fires
    mock_registry.get_valid_time_grains_for_metric.return_value = {}

    mock_extractor = MagicMock()
    mock_extractor.extract.return_value = _make_intent()

    mock_validator = MagicMock()
    mock_validator.validate.return_value = _make_validation(passed=True)
    mock_validator._get_bare_dimension.side_effect = lambda x: x

    mock_sql_gen = MagicMock()
    mock_sql_gen.generate.return_value = _make_generated_query()
    mock_sql_gen.execute_query.return_value = [
        {"plan_type": "basic", "mrr": 10000},
        {"plan_type": "premium", "mrr": 25000},
    ]

    mock_lineage_svc = MagicMock()
    mock_lineage_svc.resolve_metric.return_value = _make_lineage()

    mock_response_builder = MagicMock()
    mock_response_builder.build_success.return_value = _make_success_response()
    mock_response_builder.build_rejection.return_value = _make_rejection_response()
    mock_response_builder.build_dry_run.return_value = _make_success_response()

    mock_manifest = MagicMock()
    mock_manifest._loaded = True

    mock_settings = MagicMock()
    mock_settings.gateway_version = "1.0.0"
    mock_settings.gateway_env = "test"
    # Must be a real str: /health returns it as HealthResponse.warehouse_engine and
    # pydantic rejects a MagicMock. A bare MagicMock() attribute would 500 the
    # endpoint rather than fail a clean assertion.
    mock_settings.warehouse_engine = "duckdb"
    mock_settings.snowflake_account = "test.snowflakecomputing.com"
    mock_settings.snowflake_user = "test_user"
    mock_settings.snowflake_password = "test_password"
    mock_settings.snowflake_database = "test_db"
    mock_settings.snowflake_warehouse = "test_wh"
    mock_settings.snowflake_role = "test_role"

    mocks = {
        "registry": mock_registry,
        "extractor": mock_extractor,
        "validator": mock_validator,
        "sql_gen": mock_sql_gen,
        "lineage": mock_lineage_svc,
        "response_builder": mock_response_builder,
        "manifest": mock_manifest,
        "settings": mock_settings,
    }

    def _inject_mocks():
        """Overwrite app.state with test mocks AFTER the lifespan has initialised."""
        app.state.settings = mock_settings
        app.state.manifest_parser = mock_manifest
        app.state.metric_registry = mock_registry
        app.state.intent_extractor = mock_extractor
        app.state.semantic_validator = mock_validator
        app.state.sql_generator = mock_sql_gen
        app.state.lineage_resolver = mock_lineage_svc
        app.state.response_builder = mock_response_builder
        # Also clear out classifier and cache to avoid real LLM calls / side effects
        app.state.intent_classifier = None
        app.state.query_cache = None

    with TestClient(app, raise_server_exceptions=False) as c:
        # Lifespan has now run. Overwrite real services with mocks.
        _inject_mocks()
        yield c, mocks


# ──────────────────────────────────────────────── Tests

class TestValidQueryReturns200:
    def test_valid_query_returns_200(self, client) -> None:
        """A valid query with all mocks returning success should return HTTP 200."""
        c, mocks = client
        response = c.post(
            "/api/v1/query",
            json={"query": "What is the MRR by plan type for the last 3 months?"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "query" in data
        assert "validation" in data
        assert "governance" in data
        assert "result" in data

    def test_valid_query_includes_request_id(self, client) -> None:
        """Response should always contain a request_id field."""
        c, _ = client
        response = c.post("/api/v1/query", json={"query": "Show me MRR"})
        assert response.status_code == 200
        assert "request_id" in response.json()


class TestInvalidMetricReturns422:
    def test_uncertified_metric_returns_422(self, client) -> None:
        """An uncertified metric query should return HTTP 422 with rejection details."""
        c, mocks = client
        # Return a failed validation AND ensure the response builder returns a rejection
        mocks["validator"].validate.return_value = _make_validation(passed=False)
        from main import app
        app.state.semantic_validator = mocks["validator"]
        app.state.response_builder = mocks["response_builder"]
        mocks["response_builder"].build_rejection.return_value = _make_rejection_response()

        response = c.post(
            "/api/v1/query",
            json={"query": "What is the unknown_metric_xyz?"},
        )
        assert response.status_code == 422
        data = response.json()
        assert data["status"] == "rejected"
        assert data["validation"]["passed"] is False
        assert len(data["validation"]["violations"]) > 0

    def test_rejection_includes_suggested_fix(self, client) -> None:
        """Rejection response should include a suggested_fix."""
        c, mocks = client
        mocks["validator"].validate.return_value = _make_validation(passed=False)
        from main import app
        app.state.semantic_validator = mocks["validator"]
        mocks["response_builder"].build_rejection.return_value = _make_rejection_response()

        response = c.post("/api/v1/query", json={"query": "bad metric query"})
        assert response.status_code == 422
        data = response.json()
        assert data["rejection"]["suggested_fix"] is not None


class TestGrainMismatchReturns422:
    def test_grain_mismatch_returns_422_with_explanation(self, client) -> None:
        """Grain mismatch should return 422 with an informative message."""
        c, mocks = client
        mocks["validator"].validate.return_value = ValidationResult(
            is_valid=False,
            validation_passed=["metrics_certified", "dimensions_certified"],
            violations=[
                ViolationDetail(
                    rule="grain_compatibility",
                    severity="ERROR",
                    message=(
                        "Cannot combine 'mrr' (grain: subscription+month) with "
                        "'engagement_rate' (grain: session_id) — grain mismatch."
                    ),
                    affected_elements=["mrr", "engagement_rate"],
                )
            ],
            safe_to_execute=False,
            suggested_fix="Query each metric separately.",
        )
        mocks["response_builder"].build_rejection.return_value = GatewayResponse(
            request_id="test",
            timestamp=datetime.now(timezone.utc),
            status="rejected",
            query=QueryBlock(original="test", interpreted_metrics=["mrr", "engagement_rate"], interpreted_dimensions=[]),
            validation=ValidationBlock(
                passed=False,
                checks_run=["metrics_certified"],
                violations=[
                    ViolationDetail(
                        rule="grain_compatibility",
                        severity="ERROR",
                        message="Cannot combine 'mrr' with 'engagement_rate' — grain mismatch.",
                        affected_elements=["mrr", "engagement_rate"],
                    )
                ],
            ),
            governance=GovernanceBlock(),
            result=ResultBlock(),
            rejection=RejectionBlock(
                reason="Grain mismatch detected.",
                violated_rules=["grain_compatibility"],
                suggested_fix="Query each metric separately.",
                safe_alternatives=["expansion_mrr"],
            ),
        )

        response = c.post(
            "/api/v1/query",
            json={"query": "Show me MRR and engagement rate combined"},
        )
        assert response.status_code == 422
        data = response.json()
        assert data["status"] == "rejected"
        assert any("grain" in v["rule"].lower() or "grain" in v["message"].lower()
                   for v in data["validation"]["violations"])


class TestDryRun:
    def test_dry_run_skips_execution(self, client) -> None:
        """With dry_run=True, execute_query should never be called."""
        c, mocks = client
        mocks["validator"].validate.return_value = _make_validation(passed=True)

        response = c.post(
            "/api/v1/query",
            json={"query": "MRR by plan type", "options": {"dry_run": True}},
        )
        assert response.status_code == 200
        # execute_query must NOT have been called
        mocks["sql_gen"].execute_query.assert_not_called()


class TestHealthEndpoint:
    def test_health_endpoint_returns_200(self, client) -> None:
        """GET /api/v1/health should return 200 with required fields."""
        c, _ = client
        response = c.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["manifest_loaded"] is True
        assert data["metrics_loaded"] == 1  # one mock metric
        assert "status" in data
        assert "gateway_version" in data

    def test_health_metrics_count(self, client) -> None:
        """Health endpoint should reflect the number of loaded metrics."""
        c, mocks = client
        from main import app
        mocks["registry"].list_metrics.return_value = [_make_metric_def()] * 6
        app.state.metric_registry = mocks["registry"]
        response = c.get("/api/v1/health")
        data = response.json()
        assert data["metrics_loaded"] == 6

    def test_health_includes_subsystem_fields(self, client) -> None:
        """Health response must include chroma_db, llm_primary, llm_fallback, cache_entries fields."""
        c, _ = client
        response = c.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert "chroma_db" in data
        assert "llm_primary" in data
        assert "llm_fallback" in data
        assert "cache_entries" in data

    def test_health_status_degraded_when_no_metrics(self, client) -> None:
        """status should be 'degraded' when no metrics are loaded."""
        c, mocks = client
        from main import app
        mocks["registry"].list_metrics.return_value = []
        app.state.metric_registry = mocks["registry"]
        response = c.get("/api/v1/health")
        data = response.json()
        assert data["status"] == "degraded"


class TestMetricsEndpoint:
    def test_list_metrics_returns_all(self, client) -> None:
        """GET /api/v1/metrics should return the full metrics catalog."""
        c, mocks = client
        mocks["registry"].list_metrics.return_value = [
            _make_metric_def("mrr"),
            _make_metric_def("ltv"),
        ]
        response = c.get("/api/v1/metrics")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_get_single_metric_found(self, client) -> None:
        """GET /api/v1/metrics/mrr should return the MRR metric."""
        c, mocks = client
        mocks["registry"].get_metric.return_value = _make_metric_def("mrr")
        response = c.get("/api/v1/metrics/mrr")
        assert response.status_code == 200
        assert response.json()["name"] == "mrr"

    def test_get_single_metric_not_found(self, client) -> None:
        """GET /api/v1/metrics/unknown should return 404."""
        c, mocks = client
        mocks["registry"].get_metric.return_value = None
        response = c.get("/api/v1/metrics/nonexistent_metric")
        assert response.status_code == 404


class TestCacheClearEndpoint:
    def test_cache_clear_no_key_in_development_succeeds(self, client) -> None:
        """In development env (no ADMIN_SECRET_KEY), clear must succeed without a key."""
        c, mocks = client
        with patch("api.routes.query._settings") as mock_settings:
            mock_settings.gateway_env = "development"
            mock_settings.admin_secret_key = ""
            response = c.post("/api/v1/cache/clear")
            # Must return 200 in dev mode even with no key
            assert response.status_code == 200

    def test_cache_clear_wrong_key_returns_403(self, client) -> None:
        """In production env, a wrong X-Admin-Key must return 403."""
        c, mocks = client
        with patch("api.routes.query._settings") as mock_settings:
            mock_settings.gateway_env = "production"
            mock_settings.admin_secret_key = "correct-secret"
            response = c.post("/api/v1/cache/clear", headers={"X-Admin-Key": "wrong-key"})
            assert response.status_code == 403

    def test_cache_clear_correct_key_returns_200(self, client) -> None:
        """The correct X-Admin-Key header must clear the cache and return 200."""
        c, mocks = client
        with patch("api.routes.query._settings") as mock_settings:
            mock_settings.gateway_env = "production"
            mock_settings.admin_secret_key = "my-secret"
            response = c.post("/api/v1/cache/clear", headers={"X-Admin-Key": "my-secret"})
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"

    def test_cache_clear_no_key_in_production_returns_403(self, client) -> None:
        """In production with an ADMIN_SECRET_KEY set, missing key must return 403."""
        c, mocks = client
        with patch("api.routes.query._settings") as mock_settings:
            mock_settings.gateway_env = "production"
            mock_settings.admin_secret_key = "secret"
            response = c.post("/api/v1/cache/clear")  # no X-Admin-Key header
            assert response.status_code == 403


# ── Raw-question cache key ────────────────────────────────────────────────────
# The intent-keyed L2 cache sits AFTER the first LLM call, so a repeated question
# still paid for it: production logged `CACHE HIT - 5462.1 ms`, all of it Gemini
# re-deriving an intent it had already derived. Measured after the change:
# 11,662 ms -> 9.8 ms -> 7.2 ms for the same question three times.
#
# The risk of a raw-text key is serving one question's answer to another, so
# everything that can change the extracted intent has to be in it.


class _Opts:
    def __init__(self, max_rows=100):
        self.max_rows = max_rows


class _Msg:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class _Body:
    def __init__(self, query, history=None, dashboard_context=None, max_rows=100):
        self.query = query
        self.history = history
        self.dashboard_context = dashboard_context
        self.options = _Opts(max_rows)


class TestRawQueryCacheKey:
    def test_identical_requests_share_a_key(self) -> None:
        a = _raw_query_cache_key(_Body("What is MRR by plan type?"))
        b = _raw_query_cache_key(_Body("What is MRR by plan type?"))
        assert a == b

    def test_whitespace_and_case_are_normalised(self) -> None:
        a = _raw_query_cache_key(_Body("What is MRR by plan type?"))
        b = _raw_query_cache_key(_Body("  what   IS mrr  by PLAN type?  "))
        assert a == b

    def test_different_questions_do_not_collide(self) -> None:
        a = _raw_query_cache_key(_Body("What is MRR by plan type?"))
        b = _raw_query_cache_key(_Body("What is churn rate by country?"))
        assert a != b

    def test_history_is_part_of_the_key(self) -> None:
        """
        A fragment inherits from history, so the same text after a different
        conversation is a different question. Without this, "and for 2025?" would
        serve the previous turn's answer.
        """
        plain = _raw_query_cache_key(_Body("and for 2025?"))
        after = _raw_query_cache_key(
            _Body("and for 2025?", history=[_Msg("user", "MRR for the US")])
        )
        assert plain != after

    def test_different_history_gives_different_keys(self) -> None:
        one = _raw_query_cache_key(_Body("and for 2025?", history=[_Msg("user", "MRR for the US")]))
        two = _raw_query_cache_key(_Body("and for 2025?", history=[_Msg("user", "MRR for Germany")]))
        assert one != two

    def test_dashboard_context_is_part_of_the_key(self) -> None:
        """Only the dashboard chat sends it, and its prompt block changes the answer."""
        without = _raw_query_cache_key(_Body("What is MRR?"))
        with_ctx = _raw_query_cache_key(_Body("What is MRR?", dashboard_context={"filters": {}}))
        assert without != with_ctx

    def test_max_rows_is_part_of_the_key(self) -> None:
        """It changes the payload that would be replayed."""
        ten = _raw_query_cache_key(_Body("What is MRR?", max_rows=10))
        hundred = _raw_query_cache_key(_Body("What is MRR?", max_rows=100))
        assert ten != hundred

    def test_key_is_namespaced_away_from_the_intent_cache(self) -> None:
        """Both live in the same store; a collision would cross-serve."""
        assert _raw_query_cache_key(_Body("What is MRR?"))["type"] == "raw_query"

    def test_empty_query_does_not_raise(self) -> None:
        assert _raw_query_cache_key(_Body(""))["q"] == ""


# ─────────────────────────────────────────────────────────────────────────────
# The narrative must not describe a ranked SLICE as a distribution.
#
# `_generate_narrative` computed min/max/sum over `results`, which is already
# post-LIMIT. With limit=1 the min and max are the SAME row, and the model was
# handed `metric_value (sum of rows)` as though it were a total — so a top-1
# query produced "Canada generated the highest MRR at $8,937.76, while the
# lowest performing country was Canada with $8,937.76 … the entire recorded
# revenue for this period is concentrated within a single geographic market".
# Three claims, none of them supported by one row.
#
# This module had ZERO coverage before these tests.
# ─────────────────────────────────────────────────────────────────────────────
class TestRankedSubsetNarrative:
    """A limited result set is presented as a ranked slice, not a population."""

    AUG = TimeRange(start_date="2026-08-01", end_date="2026-08-31", relative=None)

    @staticmethod
    def _prompts(question, rows, intent):
        """Return (system_prompt, user_prompt) without calling any provider."""
        with patch("api.routes.query._chat_with_fallback") as chat:
            chat.return_value = "stub"
            _generate_narrative(question, rows, intent, object())
        assert chat.called, "narrative did not reach the provider call"
        _, system_prompt, user_prompt = chat.call_args.args[:3]
        return system_prompt, user_prompt

    def _ranked_prompts(self):
        intent = QueryIntent(
            original_query="Which country has the highest MRR in August 2026",
            metrics=["mrr"],
            dimensions=["subscriber__country"],
            time_range=self.AUG,
            order_by="mrr",
            order_direction="desc",
            limit=1,
        )
        return self._prompts(
            "Which country has the highest MRR in August 2026",
            [{"subscriber__country": "Canada", "mrr": 8937.76}],
            intent,
        )

    def test_ranked_result_is_labelled_a_subset(self):
        _, user = self._ranked_prompts()
        assert "Ranked Subset" in user
        assert "NOT the full breakdown" in user
        assert "total_segments: UNKNOWN" in user

    def test_ranked_result_hides_the_misleading_extremes_and_total(self):
        _, user = self._ranked_prompts()
        assert "Pre-computed Stats" not in user
        for leaked in ("max_value", "min_value", "metric_value"):
            assert leaked not in user, (
                f"{leaked} over a post-LIMIT slice describes the slice, not the "
                "population, and the model cannot tell the difference"
            )

    def test_ranked_result_states_its_direction(self):
        # Must assert against the Ranked Subset block, NOT a substring that the
        # echoed question ("…the highest MRR…") already satisfies — the looser
        # version passed against the pre-fix code and pinned nothing.
        _, user = self._ranked_prompts()
        assert "ranked_by: mrr (highest first)" in user
        assert "with the highest mrr" in user.lower()

    def test_ascending_rank_is_not_described_as_highest(self):
        intent = QueryIntent(
            original_query="5 plan types with the lowest retention rate",
            metrics=["retention_rate"],
            dimensions=["subscription__plan_type"],
            order_by="retention_rate",
            order_direction="asc",
            limit=5,
        )
        _, user = self._prompts(
            "5 plan types with the lowest retention rate",
            [{"subscription__plan_type": "basic", "retention_rate": 0.81}],
            intent,
        )
        assert "lowest retention_rate" in user
        assert "highest" not in user

    def test_the_three_false_claims_are_explicitly_forbidden(self):
        system, _ = self._ranked_prompts()
        assert "8. If a 'Ranked Subset' block" in system
        assert "Do NOT name a lowest or bottom segment" in system   # "lowest was Canada"
        assert "call any figure a total" in system                  # summed one row
        assert "concentrated, uniform" in system                    # "single market"

    def test_rule_one_no_longer_mandates_naming_a_bottom_segment(self):
        # Rule 1 used to demand a top AND a bottom unconditionally, which is what
        # made the model name the same row twice.
        system, _ = self._ranked_prompts()
        assert "1. If a 'Pre-computed Stats' block is present" in system

    def test_an_unapplied_limit_does_not_get_the_ranked_framing(self):
        """The governed fallback builder ignores limit/order_by and returns everything.

        intent.limit is still set on that path, so branching on it alone would
        label a full 15-row breakdown "the 1 row with the highest mrr".
        """
        intent = QueryIntent(
            original_query="Which country has the highest MRR in August 2026",
            metrics=["mrr"],
            dimensions=["subscriber__country"],
            time_range=self.AUG,
            order_by="mrr",
            order_direction="desc",
            limit=1,
        )
        _, user = self._prompts(
            "Which country has the highest MRR in August 2026",
            [
                {"subscriber__country": "US", "mrr": 73681.33},
                {"subscriber__country": "IN", "mrr": 23073.35},
                {"subscriber__country": "CA", "mrr": 8937.76},
            ],
            intent,
        )
        assert "Ranked Subset" not in user
        assert "Pre-computed Stats" in user

    def test_an_unranked_breakdown_keeps_the_full_distribution_stats(self):
        intent = QueryIntent(
            original_query="MRR by country for August 2026",
            metrics=["mrr"],
            dimensions=["subscriber__country"],
            time_range=self.AUG,
        )
        _, user = self._prompts(
            "MRR by country for August 2026",
            [
                {"subscriber__country": "Canada", "mrr": 8937.76},
                {"subscriber__country": "US", "mrr": 41022.10},
                {"subscriber__country": "DE", "mrr": 15003.44},
            ],
            intent,
        )
        assert "Ranked Subset" not in user
        assert "Pre-computed Stats" in user
        assert "max_value: 41022.1" in user and "max_label: US" in user
        assert "min_label: Canada" in user
        assert "metric_value (sum of rows): 64963.3" in user
