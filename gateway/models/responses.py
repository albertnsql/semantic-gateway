"""
models/responses.py — Pydantic v2 response models for the AI Semantic Gateway API.

Every API endpoint returns one of these models.  The GatewayResponse is the
canonical envelope that carries results, governance metadata, and rejection
details in a single consistent structure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────── Shared sub-models

class TimeRange(BaseModel):
    """Resolved time window for a query."""

    start_date: str = Field(description="Start date in YYYY-MM-DD format.")
    end_date: str = Field(description="End date in YYYY-MM-DD format.")
    relative: str | None = Field(
        default=None,
        description="Original relative expression, e.g. 'last_30_days'.",
    )


class FilterClause(BaseModel):
    """A single filter predicate extracted from the natural language query."""

    column: str
    operator: str = Field(description="eq | neq | gt | gte | lt | lte | in")
    value: str | list[str]


class ViolationDetail(BaseModel):
    """Details of a single governance rule violation."""

    rule: str = Field(description="Machine-readable rule name.")
    severity: str = Field(description="ERROR | WARNING")
    message: str = Field(description="Human-readable explanation.")
    affected_elements: list[str] = Field(
        default_factory=list,
        description="Metric/dimension names involved in the violation.",
    )


# ──────────────────────────────────────────────── GatewayResponse sub-sections

class QueryBlock(BaseModel):
    """The interpreted query, echoed back in the response."""

    original: str
    interpreted_metrics: list[str] = Field(default_factory=list)
    interpreted_dimensions: list[str] = Field(default_factory=list)
    time_range: TimeRange | None = None


class ValidationBlock(BaseModel):
    """Semantic governance validation summary."""

    passed: bool
    checks_run: list[str] = Field(default_factory=list)
    violations: list[ViolationDetail] = Field(default_factory=list)


class GovernanceBlock(BaseModel):
    """Certified metric context and lineage provenance."""

    certified_definition: str = Field(
        default="", description="Human-readable certified metric definition."
    )
    grain: str = Field(default="")
    grain_columns: list[str] = Field(default_factory=list)
    lineage_path: list[str] = Field(default_factory=list)
    source_model: str = Field(default="")
    warning: str | None = Field(
        default=None,
        description="Grain safety warning if applicable.",
    )


class ResultBlock(BaseModel):
    """Query execution results."""

    row_count: int = 0
    data: list[dict[str, Any]] = Field(default_factory=list)
    generated_sql: str = ""
    metricflow_query: str = ""


class RejectionBlock(BaseModel):
    """Populated only when status == 'rejected'."""

    reason: str | None = None
    violated_rules: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None
    safe_alternatives: list[str] = Field(default_factory=list)


# ──────────────────────────────────────────────── Root response envelope

class GatewayResponse(BaseModel):
    """
    Canonical response envelope for every gateway API call.

    Status values:
      - ``success``  — query validated, executed, results returned
      - ``rejected`` — query failed semantic governance checks
      - ``error``    — unexpected internal error
      - ``dry_run``  — validation completed, execution skipped
    """

    request_id: str = Field(description="UUID4 trace identifier.")
    timestamp: datetime = Field(description="UTC timestamp of the response.")
    status: str = Field(description="success | rejected | error | dry_run")

    query: QueryBlock
    validation: ValidationBlock
    governance: GovernanceBlock = Field(default_factory=GovernanceBlock)
    result: ResultBlock = Field(default_factory=ResultBlock)
    rejection: RejectionBlock = Field(default_factory=RejectionBlock)


# ──────────────────────────────────────────────── Health response

class HealthResponse(BaseModel):
    """Response body for GET /api/v1/health."""

    status: str = Field(description="healthy | degraded")
    snowflake_connected: bool
    manifest_loaded: bool
    metrics_loaded: int
    semantic_models_loaded: int
    gateway_version: str
    gateway_env: str

    # Subsystem detail fields — added so callers can see exactly what is degraded
    chroma_db: str = Field(
        default="unknown",
        description="ok (N metrics indexed) | empty | unavailable",
    )
    llm_primary: str = Field(
        default="unknown",
        description="ok | unavailable",
    )
    llm_fallback: str = Field(
        default="unknown",
        description="ok | unavailable",
    )
    cache_entries: int = Field(
        default=0,
        description="Number of active (non-expired) entries in the query result cache.",
    )
