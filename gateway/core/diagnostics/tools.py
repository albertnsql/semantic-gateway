"""
core/diagnostics/tools.py — the one seam between the diagnostic agent and the data.

The agent's ONLY way to reach the warehouse is `run_governed_query()`. It emits a
`QueryIntent` and this hands it to the existing chain:

    SemanticValidator -> SQLGenerator -> pool.execute()

That is the whole governance story for the diagnostic path. Nothing here writes SQL,
so every probe inherits, for free:

* the certified metric x dimension check
* `FilterClause`'s multi-value coercion (a filter left as one string renders as
  ``IN ('basic,standard')``, which is valid SQL matching nothing and returns zero
  rows with status=success)
* `assert_read_only()` inside the pool
* the L1 template cache and the L2 result cache, so overlapping diagnoses get cheap

This is also the only module in the package that touches `app.state` services.
Everything else receives data, which is what keeps `analysis.py` and the playbooks
testable with fixtures.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

from core.diagnostics.state import Finding, next_finding_id

logger = logging.getLogger(__name__)


class ProbeRejected(Exception):
    """
    The governance layer refused the probe.

    Distinct from an execution failure on purpose: a rejection means the planner
    asked for something uncertified, which is a driver-graph or prompt problem to
    fix, whereas an execution error is an infrastructure problem. Collapsing them
    would hide the first behind retries of the second.
    """

    def __init__(self, message: str, violations: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.violations = list(violations)


def build_probe_intent(
    metric: str,
    dimensions: Sequence[str] = (),
    time_range: Any | None = None,
    filters: Sequence[Any] = (),
    original_query: str = "",
) -> Any:
    """
    Construct a `QueryIntent` for a probe.

    Imported lazily so `state.py` and `analysis.py` stay free of gateway imports —
    a module-level import here would pull the extractor (and its OpenAI client) into
    every test that touches the package.

    `query_type` is pinned to ``metric_query``: a probe is a metric query by
    construction and must never be re-routed as a schema question or, worse, back
    into the diagnostic path.
    """
    from core.intent_extractor import QueryIntent

    return QueryIntent(
        original_query=original_query or f"probe: {metric} by {','.join(dimensions)}",
        metrics=[metric],
        dimensions=list(dimensions),
        filters=list(filters),
        time_range=time_range,
        query_type="metric_query",
    )


def run_governed_query(
    intent: Any,
    *,
    validator: Any,
    sql_generator: Any,
    existing: Sequence[Finding] = (),
    label: str = "",
    max_rows: int = 1000,
    query_cache: Any | None = None,
) -> Finding:
    """
    Validate, compile and execute one probe, returning a citable `Finding`.

    Args:
        intent: a `QueryIntent`, normally from `build_probe_intent`.
        validator: `app.state.semantic_validator`.
        sql_generator: `app.state.sql_generator`.
        existing: findings so far — only used to allocate the next citation id.
        label: human-readable description for the evidence table.
        max_rows: cap on returned rows. A probe is aggregated by a dimension, so
            this is a guard against a pathological cardinality, not a page size.
        query_cache: optional L2 cache. Shared with the NL path, so a probe may hit
            an entry a user query populated and vice versa.

    Returns:
        A `Finding`. Execution failures come back with `error` set rather than
        raising, because one dead probe should cost its own evidence slot and not
        the whole diagnosis.

    Raises:
        ProbeRejected: governance said no. That is a bug in what was asked, so it
            propagates instead of being swallowed as an empty finding.
    """
    finding_id = next_finding_id(existing)
    metric = intent.metrics[0] if intent.metrics else ""
    dimensions = list(intent.dimensions or [])
    description = label or f"{metric} by {', '.join(dimensions) or 'no breakdown'}"

    validation = validator.validate(intent)
    if not validation.safe_to_execute:
        violations = [v.message for v in getattr(validation, "violations", [])]
        raise ProbeRejected(
            f"probe rejected: {description} - {'; '.join(violations) or 'no detail'}",
            violations,
        )

    # Cache keyed on the intent, exactly as the NL route keys it, so the two paths
    # actually share entries instead of each keeping its own near-duplicate.
    cache_key = None
    if query_cache is not None:
        cache_key = intent.model_dump(
            mode="json", exclude={"raw_llm_response", "original_query"}
        )
        cached = query_cache.get(cache_key)
        if cached is not None:
            rows = cached.get("result", {}).get("data", []) if isinstance(cached, dict) else []
            logger.info("[%s] probe cache HIT: %s", finding_id, description)
            return Finding(
                id=finding_id, label=description, metric=metric,
                dimensions=dimensions, rows=rows[:max_rows], from_cache=True,
                intent=cache_key,
            )

    started = time.perf_counter()
    try:
        generated = sql_generator.generate(intent, validation)
        rows = sql_generator.execute_query(generated.compiled_sql)
    except Exception as exc:
        logger.warning("[%s] probe failed: %s - %s", finding_id, description, exc)
        return Finding(
            id=finding_id, label=description, metric=metric, dimensions=dimensions,
            rows=[], error=f"{type(exc).__name__}: {exc}",
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "[%s] probe ok: %s - %d row(s) in %.0f ms",
        finding_id, description, len(rows), elapsed_ms,
    )

    return Finding(
        id=finding_id,
        label=description,
        metric=metric,
        dimensions=dimensions,
        rows=list(rows[:max_rows]),
        sql=generated.compiled_sql,
        intent=intent.model_dump(mode="json", exclude={"raw_llm_response"}),
    )
