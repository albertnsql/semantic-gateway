"""
tests/test_semantic_validator.py — Unit tests for SemanticValidator.

Uses a mock MetricRegistry so these tests are fast and hermetic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.intent_extractor import FilterClause, QueryIntent, TimeRange
from core.metric_registry import MetricRegistry
from core.semantic_validator import SemanticValidator, ValidationResult
from models.semantic import MetricDefinition


# ──────────────────────────────────────────────── Fixture helpers

def _make_metric(
    name: str,
    source_model: str,
    certified_dims: list[str] | None = None,
    allowed_joins: list[str] | None = None,
    fanout_risk_models: list[str] | None = None,
    grain: str = "subscription+month",
    grain_columns: list[str] | None = None,
    time_dimension: str = "period_month",
) -> MetricDefinition:
    return MetricDefinition(
        name=name,
        label=name.replace("_", " ").title(),
        description=f"Test metric: {name}",
        metric_type="simple",
        source_model=source_model,
        grain=grain,
        grain_columns=grain_columns or ["subscription_id", "period_month"],
        certified_dimensions=certified_dims or ["plan_type", "billing_cycle", "period_month"],
        time_dimension=time_dimension,
        allowed_joins=allowed_joins or [],
        fanout_risk_models=fanout_risk_models or [],
        measure_column="mrr_usd",
        lineage=[],
        raw_yaml={},
    )


def _make_registry(metrics: list[MetricDefinition]) -> MagicMock:
    """Build a MagicMock MetricRegistry backed by the provided metric list."""
    registry = MagicMock(spec=MetricRegistry)
    metric_map = {m.name.lower(): m for m in metrics}

    registry.list_metrics.return_value = metrics
    registry.get_metric.side_effect = lambda name: metric_map.get(name.lower())
    registry.is_certified_metric.side_effect = lambda name: name.lower() in metric_map
    registry.is_certified_dimension.side_effect = lambda metric_name, dim: (
        dim.lower() in [d.lower() for d in metric_map[metric_name.lower()].certified_dimensions]
        if metric_name.lower() in metric_map else False
    )
    registry.get_dimensions_for_metric.side_effect = lambda name: (
        metric_map[name.lower()].certified_dimensions if name.lower() in metric_map else []
    )
    registry.get_grain.side_effect = lambda name: metric_map.get(name.lower(), MetricDefinition(
        name=name, label=name, description="", metric_type="simple",
        source_model="", grain="", grain_columns=[], certified_dimensions=[],
        time_dimension="", allowed_joins=[], fanout_risk_models=[],
        measure_column="", lineage=[], raw_yaml={},
    )).grain
    registry.would_cause_fanout.side_effect = lambda a, b: (
        _fanout_check(a, b, metric_map)
    )
    return registry


def _fanout_check(a: str, b: str, metric_map: dict) -> bool:
    ma = metric_map.get(a.lower())
    mb = metric_map.get(b.lower())
    if not ma or not mb:
        return False
    return mb.source_model in ma.fanout_risk_models or ma.source_model in mb.fanout_risk_models


def _make_intent(
    metrics: list[str],
    dimensions: list[str] | None = None,
    time_range: TimeRange | None = None,
    filters: list[FilterClause] | None = None,
) -> QueryIntent:
    return QueryIntent(
        original_query="test query",
        metrics=metrics,
        dimensions=dimensions or [],
        filters=filters or [],
        time_range=time_range,
    )


# ──────────────────────────────────────────────── Tests

class TestValidQuery:
    def test_valid_mrr_by_plan_type(self) -> None:
        """mrr by plan_type with a time range should pass all checks."""
        mrr = _make_metric(
            "mrr",
            "fct_mrr_monthly",
            certified_dims=["plan_type", "billing_cycle", "period_month"],
        )
        registry = _make_registry([mrr])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(
            metrics=["mrr"],
            dimensions=["plan_type"],
            time_range=TimeRange(start_date="2024-01-01", end_date="2024-03-31"),
        )
        result = validator.validate(intent)

        assert result.is_valid is True
        assert result.safe_to_execute is True
        assert len([v for v in result.violations if v.severity == "ERROR"]) == 0
        assert "metrics_certified" in result.validation_passed
        assert "dimensions_certified" in result.validation_passed

    def test_valid_query_with_no_time_range_passes_with_warning(self) -> None:
        """No time range should produce a WARNING but still pass."""
        mrr = _make_metric("mrr", "fct_mrr_monthly", certified_dims=["plan_type"])
        registry = _make_registry([mrr])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(metrics=["mrr"], dimensions=["plan_type"])
        result = validator.validate(intent)

        assert result.is_valid is True
        assert result.safe_to_execute is True
        warnings = [v for v in result.violations if v.severity == "WARNING"]
        assert any(v.rule == "time_dimension" for v in warnings)


class TestUncertifiedMetric:
    def test_unknown_metric_returns_error(self) -> None:
        """Querying an unknown metric should return an ERROR violation."""
        mrr = _make_metric("mrr", "fct_mrr_monthly")
        registry = _make_registry([mrr])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(metrics=["unknown_metric_xyz"])
        result = validator.validate(intent)

        assert result.is_valid is False
        assert result.safe_to_execute is False
        errors = [v for v in result.violations if v.severity == "ERROR"]
        assert len(errors) == 1
        assert "unknown_metric_xyz" in errors[0].message
        assert errors[0].rule == "metrics_certified"


class TestGrainMismatch:
    def test_mrr_plus_engagement_rate_returns_grain_mismatch(self) -> None:
        """Combining mrr (subscription+month grain) with engagement_rate (session grain) → ERROR."""
        mrr = _make_metric(
            "mrr",
            "fct_mrr_monthly",
            grain="subscription+month",
            allowed_joins=["expansion_mrr"],
            fanout_risk_models=["fct_stream_sessions"],
        )
        engagement_rate = _make_metric(
            "engagement_rate",
            "fct_stream_sessions",
            grain="session_id",
            fanout_risk_models=["fct_mrr_monthly"],
        )
        registry = _make_registry([mrr, engagement_rate])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(metrics=["mrr", "engagement_rate"])
        result = validator.validate(intent)

        assert result.is_valid is False
        assert result.safe_to_execute is False
        # Should be blocked by either fanout_risk or grain_compatibility
        error_rules = {v.rule for v in result.violations if v.severity == "ERROR"}
        assert error_rules & {"grain_compatibility", "fanout_risk"}

    def test_grain_mismatch_message_mentions_both_metrics(self) -> None:
        """The grain mismatch error message must name both incompatible metrics."""
        mrr = _make_metric("mrr", "fct_mrr_monthly", grain="subscription+month")
        engagement_rate = _make_metric(
            "engagement_rate",
            "fct_stream_sessions",
            grain="session_id",
            fanout_risk_models=["fct_mrr_monthly"],
        )
        registry = _make_registry([mrr, engagement_rate])

        # Override would_cause_fanout to return True for this pair
        registry.would_cause_fanout.side_effect = lambda a, b: True

        validator = SemanticValidator(registry=registry)
        intent = _make_intent(metrics=["mrr", "engagement_rate"])
        result = validator.validate(intent)

        assert result.is_valid is False


class TestFanoutCaught:
    def test_cross_grain_join_returns_fanout_error(self) -> None:
        """A known fanout pair should be caught with a clear ERROR message."""
        mrr = _make_metric(
            "mrr",
            "fct_mrr_monthly",
            fanout_risk_models=["fct_stream_sessions"],
            allowed_joins=[],
        )
        engagement = _make_metric(
            "engagement_rate",
            "fct_stream_sessions",
            fanout_risk_models=["fct_mrr_monthly"],
            allowed_joins=[],
        )
        registry = _make_registry([mrr, engagement])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(metrics=["mrr", "engagement_rate"])
        result = validator.validate(intent)

        assert result.safe_to_execute is False
        error_rules = {v.rule for v in result.violations if v.severity == "ERROR"}
        assert "fanout_risk" in error_rules or "grain_compatibility" in error_rules


class TestUncertifiedDimension:
    def test_uncertified_dimension_returns_error(self) -> None:
        """Requesting a dimension not certified for the metric → ERROR."""
        mrr = _make_metric(
            "mrr",
            "fct_mrr_monthly",
            certified_dims=["plan_type", "billing_cycle"],
        )
        registry = _make_registry([mrr])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(
            metrics=["mrr"],
            dimensions=["totally_made_up_column"],
            time_range=TimeRange(start_date="2024-01-01", end_date="2024-03-31"),
        )
        result = validator.validate(intent)

        assert result.is_valid is False
        errors = [v for v in result.violations if v.severity == "ERROR"]
        assert any(v.rule == "dimensions_certified" for v in errors)
        assert any("totally_made_up_column" in v.message for v in errors)


class TestFilterSafety:
    def test_raw_table_filter_produces_warning(self) -> None:
        """A filter referencing stream_sessions.column should produce a WARNING."""
        mrr = _make_metric("mrr", "fct_mrr_monthly", certified_dims=["plan_type"])
        registry = _make_registry([mrr])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(
            metrics=["mrr"],
            dimensions=["plan_type"],
            filters=[FilterClause(column="stream_sessions.completion_pct", operator="gt", value="0.5")],
            time_range=TimeRange(start_date="2024-01-01", end_date="2024-03-31"),
        )
        result = validator.validate(intent)

        # Should pass overall (WARNING doesn't block)
        assert result.is_valid is True
        warnings = [v for v in result.violations if v.severity == "WARNING"]
        assert any(v.rule == "filter_safety" for v in warnings)


class TestMultipleMetricValidPass:
    def test_allowed_join_passes_grain_check(self) -> None:
        """mrr + expansion_mrr (same source model) should pass grain check."""
        mrr = _make_metric(
            "mrr",
            "fct_mrr_monthly",
            allowed_joins=["expansion_mrr"],
        )
        expansion_mrr = _make_metric(
            "expansion_mrr",
            "fct_mrr_monthly",
            allowed_joins=["mrr"],
        )
        registry = _make_registry([mrr, expansion_mrr])
        # Same source model → not a fanout
        registry.would_cause_fanout.return_value = False
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(
            metrics=["mrr", "expansion_mrr"],
            dimensions=["plan_type"],
            time_range=TimeRange(start_date="2024-01-01", end_date="2024-03-31"),
        )
        result = validator.validate(intent)
        assert result.is_valid is True


class TestDimensionErrorShowsAllMetricDims:
    """
    Fix #17: When a dimension is rejected, the error must show certified dims
    from ALL queried metrics, not just the first one.
    """

    def test_error_lists_dims_from_all_metrics(self) -> None:
        """Dimension error message must mention dims from mrr AND ltv."""
        mrr = _make_metric("mrr", "fct_mrr_monthly", certified_dims=["plan_type", "billing_cycle"])
        ltv = _make_metric("ltv", "fct_payments", certified_dims=["payment_method", "currency"])
        registry = _make_registry([mrr, ltv])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(
            metrics=["mrr", "ltv"],
            dimensions=["totally_unknown_dim"],
            time_range=TimeRange(start_date="2024-01-01", end_date="2024-03-31"),
        )
        result = validator.validate(intent)

        assert result.is_valid is False
        errors = [v for v in result.violations if v.rule == "dimensions_certified"]
        assert len(errors) == 1
        err_msg = errors[0].message
        # Message must reference BOTH metrics
        assert "mrr" in err_msg and "ltv" in err_msg
        # Message must include certified dims from both metrics
        assert "plan_type" in err_msg or "billing_cycle" in err_msg
        assert "payment_method" in err_msg or "currency" in err_msg

    def test_certified_dim_for_second_metric_passes(self) -> None:
        """Dimension certified for the second metric (not the first) should still pass."""
        mrr = _make_metric("mrr", "fct_mrr_monthly", certified_dims=["plan_type"])
        ltv = _make_metric("ltv", "fct_payments", certified_dims=["payment_method", "country"])
        registry = _make_registry([mrr, ltv])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(
            metrics=["mrr", "ltv"],
            dimensions=["country"],  # only certified for ltv, not mrr
            time_range=TimeRange(start_date="2024-01-01", end_date="2024-03-31"),
        )
        result = validator.validate(intent)
        # 'country' IS certified for one of the queried metrics → no error
        dim_errors = [v for v in result.violations if v.rule == "dimensions_certified"]
        assert len(dim_errors) == 0


class TestWarningsDoNotBlockExecution:
    def test_time_dimension_warning_is_not_blocking(self) -> None:
        """WARNING violations must not set safe_to_execute=False."""
        mrr = _make_metric("mrr", "fct_mrr_monthly", certified_dims=["plan_type"])
        registry = _make_registry([mrr])
        validator = SemanticValidator(registry=registry)

        # No time_range provided → should trigger time_dimension WARNING
        intent = _make_intent(metrics=["mrr"], dimensions=["plan_type"])
        result = validator.validate(intent)

        warnings = [v for v in result.violations if v.severity == "WARNING"]
        assert any(v.rule == "time_dimension" for v in warnings)
        # Despite the warning the query should still be allowed to execute
        assert result.safe_to_execute is True

    def test_filter_safety_warning_does_not_reject_query(self) -> None:
        """A raw-table filter produces WARNING but must not block execution."""
        mrr = _make_metric("mrr", "fct_mrr_monthly", certified_dims=["plan_type"])
        registry = _make_registry([mrr])
        validator = SemanticValidator(registry=registry)

        intent = _make_intent(
            metrics=["mrr"],
            dimensions=["plan_type"],
            filters=[FilterClause(column="fct_mrr_monthly.mrr_usd", operator="gt", value="0")],
            time_range=TimeRange(start_date="2024-01-01", end_date="2024-03-31"),
        )
        result = validator.validate(intent)
        filter_warnings = [v for v in result.violations if v.rule == "filter_safety"]
        assert len(filter_warnings) > 0
        assert result.safe_to_execute is True
