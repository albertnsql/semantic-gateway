"""
core/semantic_validator.py — Semantic governance engine.

Single responsibility: enforce every governance rule on a QueryIntent
before any SQL is generated.  This is the gatekeeper that prevents
hallucinated joins, grain mismatches, and unsafe filter patterns.

Nothing in this module touches OpenAI, Snowflake, or the filesystem.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from models.responses import ViolationDetail

if TYPE_CHECKING:
    from core.intent_extractor import QueryIntent
    from core.metric_registry import MetricRegistry

logger = logging.getLogger(__name__)


class ValidationResult(BaseModel):
    """
    Complete result from a semantic validation pass.

    ``is_valid`` and ``safe_to_execute`` are both True only when there are
    zero ERROR-level violations.  WARNING-level violations do not block
    execution.
    """

    is_valid: bool
    validation_passed: list[str]
    violations: list[ViolationDetail]
    safe_to_execute: bool
    suggested_fix: str | None


class SemanticValidator:
    """
    Governance core.  Every QueryIntent passes through ``validate()`` before
    any SQL is generated or Snowflake is touched.

    Rules enforced (in execution order):
      1. check_metrics_certified     — all requested metrics must be in registry
      2. check_dimensions_certified  — all requested dims must be certified per metric
      3. check_grain_compatibility   — multi-metric queries must share a grain
      4. check_fanout_risk           — known dangerous joins are blocked
      5. check_time_dimension        — warn if no time window specified
      6. check_filter_safety         — warn on raw table column references

    Usage::

        validator = SemanticValidator(registry)
        result = validator.validate(intent)
        if not result.safe_to_execute:
            return ResponseBuilder.build_rejection(intent, result)
    """

    def __init__(self, registry: "MetricRegistry") -> None:
        self._registry = registry

    # ──────────────────────────────────────────────── public

    def validate(self, intent: "QueryIntent") -> ValidationResult:
        """
        Orchestrate all governance checks and return a complete ValidationResult.

        Stops accumulating ERROR-level violations after the first check that
        produces one — it is pointless to continue when the query is
        fundamentally unsafe.  WARNING-level violations are always collected.

        Args:
            intent: Extracted query intent from IntentExtractor.

        Returns:
            :class:`ValidationResult` with full pass/violation detail.
        """
        all_violations: list[ViolationDetail] = []
        passed: list[str] = []

        checks = [
            ("metrics_certified",    self.check_metrics_certified),
            ("dimensions_certified", self.check_dimensions_certified),
            ("grain_compatibility",  self.check_grain_compatibility),
            ("fanout_risk",         self.check_fanout_risk),
            ("time_dimension",       self.check_time_dimension),
            ("filter_safety",        self.check_filter_safety),
        ]

        for check_name, check_fn in checks:
            violations = check_fn(intent)
            errors = [v for v in violations if v.severity == "ERROR"]
            warnings = [v for v in violations if v.severity == "WARNING"]

            all_violations.extend(violations)

            if errors:
                # Stop at first ERROR-producing check
                logger.warning(
                    "Semantic validation FAILED at check '%s' with %d error(s).",
                    check_name,
                    len(errors),
                )
                is_valid = False
                safe_to_execute = False
                suggested_fix = errors[0].message
                return ValidationResult(
                    is_valid=is_valid,
                    validation_passed=passed,
                    violations=all_violations,
                    safe_to_execute=safe_to_execute,
                    suggested_fix=suggested_fix,
                )
            else:
                passed.append(check_name)
                if warnings:
                    logger.debug(
                        "Check '%s' passed with %d warning(s).", check_name, len(warnings)
                    )

        logger.info(
            "Semantic validation PASSED for metrics=%s dims=%s.",
            intent.metrics,
            intent.dimensions,
        )
        return ValidationResult(
            is_valid=True,
            validation_passed=passed,
            violations=all_violations,
            safe_to_execute=True,
            suggested_fix=None,
        )

    def check_metrics_certified(
        self, intent: "QueryIntent"
    ) -> list[ViolationDetail]:
        """
        Verify every metric in intent.metrics exists in the MetricRegistry.

        Returns ERROR-level violation for each uncertified metric found.
        """
        violations: list[ViolationDetail] = []
        available = [m.name for m in self._registry.list_metrics()]

        for metric_name in intent.metrics:
            if not self._registry.is_certified_metric(metric_name):
                violations.append(
                    ViolationDetail(
                        rule="metrics_certified",
                        severity="ERROR",
                        message=(
                            f"Metric '{metric_name}' is not a certified metric. "
                            f"Available metrics: {available}."
                        ),
                        affected_elements=[metric_name],
                    )
                )
        return violations

    def _get_bare_dimension(self, dim: str) -> str:
        """
        Strips MetricFlow entity prefixes and time granularities.
        'subscriber__plan_type' → 'plan_type'
        'subscription__period_month__month' → 'period_month'
        """
        parts = dim.split('__')
        return parts[1] if len(parts) >= 2 else dim

    def check_dimensions_certified(
        self, intent: "QueryIntent"
    ) -> list[ViolationDetail]:
        """
        Verify every dimension in intent.dimensions is certified for at least
        one of the requested metrics.
        """
        violations: list[ViolationDetail] = []

        for dim in intent.dimensions:
            bare_dim = self._get_bare_dimension(dim)
            certified_for_any = False
            
            for metric_name in intent.metrics:
                if self._registry.is_certified_dimension(metric_name, bare_dim):
                    certified_for_any = True
                    break

            if not certified_for_any and intent.metrics:
                all_certified: list[str] = []
                for mn in intent.metrics:
                    if self._registry.is_certified_metric(mn):
                        all_certified.extend(self._registry.get_dimensions_for_metric(mn))
                
                # Deduplicate while preserving order
                seen: set[str] = set()
                deduped = [d for d in all_certified if not (d in seen or seen.add(d))]

                violations.append(
                    ViolationDetail(
                        rule="dimensions_certified",
                        severity="ERROR",
                        message=(
                            f"Dimension '{dim}' (bare: '{bare_dim}') is not certified for any of the requested "
                            f"metrics ({', '.join(intent.metrics)}). "
                            f"Certified dimensions available: {deduped}."
                        ),
                        affected_elements=[dim] + intent.metrics,
                    )
                )
        return violations
    def check_grain_compatibility(
        self, intent: "QueryIntent"
    ) -> list[ViolationDetail]:
        """
        If the query requests multiple metrics, verify their grains are
        compatible.

        Two metrics are grain-compatible if they share the same source model
        OR one is listed in the other's allowed_joins.

        Returns ERROR-level violation for each incompatible pair.
        """
        violations: list[ViolationDetail] = []
        certified = [m for m in intent.metrics if self._registry.is_certified_metric(m)]

        for i in range(len(certified)):
            for j in range(i + 1, len(certified)):
                m_a = certified[i]
                m_b = certified[j]
                if not self._grains_compatible(m_a, m_b):
                    grain_a = self._registry.get_grain(m_a)
                    grain_b = self._registry.get_grain(m_b)
                    violations.append(
                        ViolationDetail(
                            rule="grain_compatibility",
                            severity="ERROR",
                            message=(
                                f"Cannot combine '{m_a}' (grain: {grain_a}) with "
                                f"'{m_b}' (grain: {grain_b}) — this would produce "
                                "incorrect results due to grain mismatch. "
                                "Query each metric separately."
                            ),
                            affected_elements=[m_a, m_b],
                        )
                    )
        return violations

    def check_fanout_risk(
        self, intent: "QueryIntent"
    ) -> list[ViolationDetail]:
        """
        Check if any pair of requested metrics would cause row multiplication
        (fanout) when joined.

        Returns ERROR-level violation with the safe alternative if detected.
        """
        violations: list[ViolationDetail] = []
        certified = [m for m in intent.metrics if self._registry.is_certified_metric(m)]

        for i in range(len(certified)):
            for j in range(i + 1, len(certified)):
                m_a = certified[i]
                m_b = certified[j]
                if self._registry.would_cause_fanout(m_a, m_b):
                    metric_a_def = self._registry.get_metric(m_a)
                    metric_b_def = self._registry.get_metric(m_b)
                    model_a = metric_a_def.source_model if metric_a_def else m_a
                    model_b = metric_b_def.source_model if metric_b_def else m_b
                    grain_a = metric_a_def.grain if metric_a_def else "unknown"

                    # Suggest the allowed joins as safe alternatives
                    safe_alts = (
                        metric_a_def.allowed_joins if metric_a_def else []
                    )
                    violations.append(
                        ViolationDetail(
                            rule="fanout_risk",
                            severity="ERROR",
                            message=(
                                f"Joining '{model_a}' to '{model_b}' would cause "
                                f"a fanout at the {grain_a} grain. "
                                "This is a known dangerous join in this data model. "
                                f"Use metric '{safe_alts[0] if safe_alts else 'each metric separately'}' "
                                "instead."
                            ),
                            affected_elements=[m_a, m_b],
                        )
                    )
        return violations

    def check_time_dimension(
        self, intent: "QueryIntent"
    ) -> list[ViolationDetail]:
        """
        Warn if no time dimension or time range is specified.

        Time-unbounded queries against large fact tables are expensive and
        often accidental.  This is a WARNING (does not block execution).
        """
        violations: list[ViolationDetail] = []

        if intent.time_range is None and not intent.aggregation_level:
            # Find the default time dimension to suggest
            default_time = ""
            for metric_name in intent.metrics:
                metric = self._registry.get_metric(metric_name)
                if metric and metric.time_dimension:
                    default_time = metric.time_dimension
                    break

            suggestion = (
                f"Consider adding a time range filter using '{default_time}'."
                if default_time
                else "Consider adding a time range filter."
            )
            violations.append(
                ViolationDetail(
                    rule="time_dimension",
                    severity="WARNING",
                    message=(
                        "No time range specified. This query may scan large "
                        "amounts of data. " + suggestion
                    ),
                    affected_elements=intent.metrics,
                )
            )
        return violations

    def check_filter_safety(
        self, intent: "QueryIntent"
    ) -> list[ViolationDetail]:
        """
        Warn if any filter references a raw column that bypasses the semantic
        layer.

        Heuristic: flag filters whose column contains a table name prefix
        (e.g. ``stream_sessions.completion_pct``) — these are raw table
        references, not semantic dimensions.
        """
        violations: list[ViolationDetail] = []
        raw_patterns = [
            "stream_sessions.",
            "fct_",
            "stg_",
            "raw.",
            "int_",
        ]
        for f in intent.filters:
            col = f.column.lower()
            for pattern in raw_patterns:
                if pattern in col:
                    violations.append(
                        ViolationDetail(
                            rule="filter_safety",
                            severity="WARNING",
                            message=(
                                f"Filter on '{f.column}' appears to reference a "
                                "raw table column directly, bypassing the semantic "
                                "layer. Use a certified dimension instead."
                            ),
                            affected_elements=[f.column],
                        )
                    )
                    break
        return violations

    # ──────────────────────────────────────────────── private

    def _grains_compatible(self, metric_a: str, metric_b: str) -> bool:
        """
        Return True if metric_a and metric_b can be safely combined.

        Criteria (any one sufficient):
          - Same source model
          - metric_b is in metric_a's allowed_joins
          - Their source models differ but no fanout risk exists
        """
        m_a = self._registry.get_metric(metric_a)
        m_b = self._registry.get_metric(metric_b)
        if m_a is None or m_b is None:
            return True  # uncertified metrics are caught by check_metrics_certified

        # Same source → trivially compatible
        if m_a.source_model == m_b.source_model:
            return True

        # Explicitly allowed join
        if metric_b.lower() in [x.lower() for x in m_a.allowed_joins]:
            return True

        # Fanout = definitely incompatible
        if self._registry.would_cause_fanout(metric_a, metric_b):
            return False

        return True
