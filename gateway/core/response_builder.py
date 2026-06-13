"""
core/response_builder.py — Final API response assembler.

Single responsibility: take outputs from IntentExtractor, SemanticValidator,
SQLGenerator, and LineageResolver and assemble a single GatewayResponse.

No business logic lives here — this module only combines and formats.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from models.responses import (
    GatewayResponse,
    GovernanceBlock,
    QueryBlock,
    RejectionBlock,
    ResultBlock,
    TimeRange,
    ValidationBlock,
)

if TYPE_CHECKING:
    from core.intent_extractor import QueryIntent
    from core.lineage_resolver import LineageTrace
    from core.semantic_validator import ValidationResult
    from core.sql_generator import GeneratedQuery
    from core.metric_registry import MetricRegistry

logger = logging.getLogger(__name__)


class ResponseBuilder:
    """
    Assembles the final GatewayResponse from all upstream service outputs.

    Two factory methods:
      - :meth:`build_success` — for validated, executed queries
      - :meth:`build_rejection` — for governance-rejected queries

    Usage::

        builder = ResponseBuilder(metric_registry)
        response = builder.build_success(intent, validation, query, results, lineage)
        # or
        response = builder.build_rejection(intent, validation)
    """

    def __init__(self, metric_registry: "MetricRegistry") -> None:
        self._registry = metric_registry

    # ──────────────────────────────────────────────── public

    def build_success(
        self,
        intent: "QueryIntent",
        validation: "ValidationResult",
        query: "GeneratedQuery",
        results: list[dict[str, Any]],
        lineage: "LineageTrace | None",
    ) -> GatewayResponse:
        """
        Assemble a successful GatewayResponse.

        Args:
            intent: The extracted query intent.
            validation: Passed ValidationResult.
            query: The MetricFlow GeneratedQuery.
            results: List of data rows from Snowflake.
            lineage: Resolved LineageTrace (may be None on error).

        Returns:
            :class:`GatewayResponse` with status='success'.
        """
        request_id = str(uuid.uuid4())
        logger.info("Building success response for request_id=%s.", request_id)

        time_range = self._build_time_range(intent)

        query_block = QueryBlock(
            original=intent.original_query,
            interpreted_metrics=intent.metrics,
            interpreted_dimensions=intent.dimensions,
            time_range=time_range,
        )

        validation_block = ValidationBlock(
            passed=True,
            checks_run=validation.validation_passed,
            violations=validation.violations,
        )

        governance_block = self._build_governance_block(intent, lineage)

        result_block = ResultBlock(
            row_count=len(results),
            data=results,
            generated_sql=query.compiled_sql,
            metricflow_query=query.metricflow_query,
        )

        return GatewayResponse(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc),
            status="success",
            query=query_block,
            validation=validation_block,
            governance=governance_block,
            result=result_block,
            rejection=RejectionBlock(),
        )

    def build_dry_run(
        self,
        intent: "QueryIntent",
        validation: "ValidationResult",
        query: "GeneratedQuery",
        lineage: "LineageTrace | None",
    ) -> GatewayResponse:
        """
        Assemble a dry-run GatewayResponse (validation done, no execution).

        Args:
            intent: The extracted query intent.
            validation: Passed ValidationResult.
            query: The MetricFlow GeneratedQuery (--explain only).
            lineage: Resolved LineageTrace.

        Returns:
            :class:`GatewayResponse` with status='dry_run'.
        """
        request_id = str(uuid.uuid4())
        logger.info("Building dry_run response for request_id=%s.", request_id)

        time_range = self._build_time_range(intent)

        return GatewayResponse(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc),
            status="dry_run",
            query=QueryBlock(
                original=intent.original_query,
                interpreted_metrics=intent.metrics,
                interpreted_dimensions=intent.dimensions,
                time_range=time_range,
            ),
            validation=ValidationBlock(
                passed=True,
                checks_run=validation.validation_passed,
                violations=validation.violations,
            ),
            governance=self._build_governance_block(intent, lineage),
            result=ResultBlock(
                row_count=0,
                data=[],
                generated_sql=query.compiled_sql,
                metricflow_query=query.metricflow_query,
            ),
            rejection=RejectionBlock(),
        )

    def build_rejection(
        self,
        intent: "QueryIntent",
        validation: "ValidationResult",
    ) -> GatewayResponse:
        """
        Assemble a rejected GatewayResponse.

        Args:
            intent: The extracted query intent (may be partially populated).
            validation: Failed ValidationResult with violation details.

        Returns:
            :class:`GatewayResponse` with status='rejected'.
        """
        request_id = str(uuid.uuid4())
        logger.warning(
            "Building rejection response for request_id=%s. violations=%d",
            request_id,
            len(validation.violations),
        )

        errors = [v for v in validation.violations if v.severity == "ERROR"]
        violated_rules = [v.rule for v in errors]
        reason = validation.suggested_fix or (errors[0].message if errors else "Unknown violation.")

        # Suggest safe alternatives from the first valid metric's allowed_joins
        safe_alternatives: list[str] = []
        for metric_name in intent.metrics:
            metric = self._registry.get_metric(metric_name)
            if metric:
                safe_alternatives.extend(metric.allowed_joins)
        safe_alternatives = list(dict.fromkeys(safe_alternatives))[:3]  # dedupe + cap

        time_range = self._build_time_range(intent)

        return GatewayResponse(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc),
            status="rejected",
            query=QueryBlock(
                original=intent.original_query,
                interpreted_metrics=intent.metrics,
                interpreted_dimensions=intent.dimensions,
                time_range=time_range,
            ),
            validation=ValidationBlock(
                passed=False,
                checks_run=validation.validation_passed,
                violations=validation.violations,
            ),
            governance=GovernanceBlock(),
            result=ResultBlock(),
            rejection=RejectionBlock(
                reason=reason,
                violated_rules=violated_rules,
                suggested_fix=validation.suggested_fix,
                safe_alternatives=safe_alternatives,
            ),
        )

    # ──────────────────────────────────────────────── private

    def _build_governance_block(
        self,
        intent: "QueryIntent",
        lineage: "LineageTrace | None",
    ) -> GovernanceBlock:
        """Build the governance block from registry and lineage data."""
        certified_def = ""
        grain = ""
        grain_columns: list[str] = []
        lineage_path: list[str] = []
        source_model = ""
        warning: str | None = None

        primary_metric = intent.metrics[0] if intent.metrics else None
        if primary_metric:
            metric = self._registry.get_metric(primary_metric)
            if metric:
                grain = metric.grain
                grain_columns = metric.grain_columns
                source_model = metric.source_model
                certified_def = (
                    f"{metric.label} is defined as: {metric.description}. "
                    f"Grain: {metric.grain}. "
                    f"Certified source: {metric.source_model}."
                )

        if lineage:
            lineage_path = [step.model_name for step in lineage.transformation_steps]

        # Grain safety warning for multi-metric queries
        if len(intent.metrics) > 1:
            warning = (
                "Multiple metrics requested. Grain compatibility was validated. "
                "Review the lineage path to confirm join safety."
            )

        return GovernanceBlock(
            certified_definition=certified_def,
            grain=grain,
            grain_columns=grain_columns,
            lineage_path=lineage_path,
            source_model=source_model,
            warning=warning,
        )

    def _build_time_range(self, intent: "QueryIntent") -> TimeRange | None:
        """Convert intent.time_range to a response TimeRange model."""
        if intent.time_range is None:
            return None
        return TimeRange(
            start_date=intent.time_range.start_date,
            end_date=intent.time_range.end_date,
            relative=intent.time_range.relative,
        )
