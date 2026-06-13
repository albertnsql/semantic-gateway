"""
models/requests.py — Pydantic v2 request models for the AI Semantic Gateway API.

These models validate and document every inbound request body.
Route handlers accept these types directly via FastAPI dependency injection.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

class Message(BaseModel):
    """A single turn in the conversational history."""
    role: str
    content: str


class QueryOptions(BaseModel):
    """Optional query-execution modifiers."""

    max_rows: int = Field(default=1000, ge=1, le=10_000, description="Max rows to return.")
    include_sql: bool = Field(default=True, description="Include generated SQL in response.")
    include_lineage: bool = Field(
        default=True, description="Include lineage trace in the governance block."
    )
    dry_run: bool = Field(
        default=False,
        description="Validate and plan the query but skip warehouse execution.",
    )


class QueryRequest(BaseModel):
    """
    Top-level request body for POST /api/v1/query.

    The ``query`` field is a natural-language analytics question.
    The gateway will extract intent, validate governance, generate
    MetricFlow SQL, and return enriched results.
    """

    query: str = Field(
        min_length=3,
        max_length=2000,
        description="Natural language analytics question.",
        examples=["What is the MRR by plan type for the last 3 months?"],
    )
    history: list[Message] = Field(
        default_factory=list,
        description="Optional conversational history to resolve context."
    )
    dashboard_context: dict | None = Field(
        default=None,
        description="Optional dashboard context (active filters, visible widgets) from the frontend."
    )
    options: QueryOptions = Field(default_factory=QueryOptions)
