"""
core/dimension_values.py — the actual values each filterable dimension holds.

Why this exists
---------------
The prompt tells the LLM which dimension NAMES are certified but never which VALUES
exist, so it emits whatever the user said. Asked "why is revenue lower in Germany" the
live model returns::

    filters=[('subscriber__country', 'Germany')]

while the warehouse stores ISO codes — ``DE``. The filter matches nothing, the query
returns zero rows with ``status=success``, and the narrative describes an empty result
as an answer. No error anywhere. Same shape as the ``IN ('basic,standard')`` bug the
``FilterClause`` coercion was written for: valid SQL that matches nothing.

It affects the ordinary metric path as much as diagnostics — "revenue in Germany" has
always failed this way.

Why it is queried, not hardcoded
--------------------------------
A hardcoded list drifts the first time the data is regenerated, and drifting silently
is the whole problem. Measured cost: 14 ``SELECT DISTINCT`` on indexed low-cardinality
columns, all 80 values, well under a second at startup.

Cardinality is what makes prompt injection viable at all — 80 values total across
every filterable dimension, a few hundred tokens. That is a measurement, not an
assumption, and it is worth re-checking before adding a dimension here: a
high-cardinality column (subscriber_id, content title) must NOT be added, because the
prompt would balloon and the model would start guessing from a truncated list.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Bare dimension name -> (table, column). Only LOW-CARDINALITY dimensions a user
# plausibly filters on. Verified 2026-08-28: 80 distinct values in total.
#
# `cohort_month`, `signup_date` and the other date columns are deliberately absent —
# they are handled by time_range, not by value matching, and they are unbounded.
_SOURCES: dict[str, tuple[str, str]] = {
    "country":             ("marts.dim_subscribers", "country"),
    "plan_type":           ("marts.dim_subscribers", "plan_type"),
    "acquisition_channel": ("marts.dim_subscribers", "acquisition_channel"),
    "age_group":           ("marts.dim_subscribers", "age_group"),
    "subscription_status": ("marts.dim_subscribers", "subscription_status"),
    "churn_reason":        ("marts.dim_subscribers", "churn_reason"),
    "billing_cycle":       ("marts.fct_mrr_monthly", "billing_cycle"),
    "mrr_type":            ("marts.fct_mrr_monthly", "mrr_type"),
    "payment_method":      ("marts.fct_payments", "payment_method"),
    "currency":            ("marts.fct_payments", "currency"),
    "device_type":         ("marts.fct_stream_sessions", "device_type"),
    "referral_source":     ("marts.fct_stream_sessions", "referral_source"),
    "quality_streamed":    ("marts.fct_stream_sessions", "quality_streamed"),
    "watch_quality_tier":  ("marts.fct_stream_sessions", "watch_quality_tier"),
    "recommendation_type": ("staging.stg_recommendation_events", "recommendation_type"),
}

# A dimension yielding more than this is dropped rather than truncated. A truncated
# list is worse than none: the model treats it as exhaustive and confidently picks a
# wrong value from the visible subset.
_MAX_VALUES = 40


def load(pool: Any) -> dict[str, list[str]]:
    """
    Read the distinct values of every filterable dimension.

    Best-effort by design. A failed query drops that one dimension and the prompt
    simply says nothing about it, which is exactly today's behaviour — so this can
    never make things worse than not having it.

    Returns ``{bare_dimension: [values]}``, alphabetically sorted for a stable prompt
    (an unstable prompt would defeat the raw-question cache at Stage 0).
    """
    if pool is None:
        return {}

    values: dict[str, list[str]] = {}
    for dimension, (table, column) in _SOURCES.items():
        try:
            rows = pool.execute(
                f"SELECT DISTINCT {column} AS v FROM {table} "
                f"WHERE {column} IS NOT NULL ORDER BY 1"
            )
        except Exception as exc:
            logger.warning("dimension values: %s.%s unavailable (%s)", table, column, exc)
            continue

        # DuckDBPool.execute() uppercases keys to preserve Snowflake DictCursor
        # behaviour (CLAUDE.md, DuckDB section, point 4).
        found = [
            str(row[key]) for row in rows
            for key in row
            if key.lower() == "v" and row[key] is not None
        ]
        if not found:
            continue
        if len(found) > _MAX_VALUES:
            logger.info(
                "dimension values: %s has %d values (> %d) - omitting from the prompt "
                "rather than truncating", dimension, len(found), _MAX_VALUES,
            )
            continue
        values[dimension] = found

    logger.info(
        "Dimension values loaded: %d dimension(s), %d value(s) total.",
        len(values), sum(len(v) for v in values.values()),
    )
    return values


def format_for_prompt(values: dict[str, list[str]]) -> str:
    """
    Render the allowed values as a prompt block.

    Empty string when nothing loaded, so the caller can inject unconditionally and
    the prompt is unchanged from today when the warehouse is unreachable.
    """
    if not values:
        return ""

    lines = [
        "## ALLOWED FILTER VALUES",
        "When a filter narrows one of these dimensions, the value MUST be taken from",
        "this list verbatim. Translate what the user said into the stored value:",
        '"Germany" -> "DE", "credit card" -> "card", "annual plan" -> "annual".',
        "A value outside these lists matches zero rows and returns an empty answer,",
        "so if the user names something absent here, set needs_clarification instead",
        "of guessing.",
        "",
    ]
    for dimension in sorted(values):
        lines.append(f"  {dimension}: {', '.join(values[dimension])}")
    return "\n".join(lines) + "\n"


def unknown_filter_values(
    filters: Any, values: dict[str, list[str]]
) -> list[tuple[str, str]]:
    """
    Filter values that are not in the known set for their dimension.

    Returns ``[(column, value)]``. Reported rather than corrected: a fuzzy match would
    silently answer a different question than the one asked, which is the failure this
    module exists to stop, not a fix for it.

    A dimension with no loaded values is skipped — absence of data is not evidence
    that a value is wrong.
    """
    unknown: list[tuple[str, str]] = []
    for clause in filters or []:
        column = str(getattr(clause, "column", ""))
        if not column:
            continue
        bare = column.rsplit("__", 1)[-1].lower()
        allowed = values.get(bare)
        if not allowed:
            continue
        allowed_lower = {a.lower() for a in allowed}
        raw = getattr(clause, "value", None)
        candidates = raw if isinstance(raw, (list, tuple)) else [raw]
        for candidate in candidates:
            if candidate is None:
                continue
            if str(candidate).lower() not in allowed_lower:
                unknown.append((column, str(candidate)))
    return unknown
