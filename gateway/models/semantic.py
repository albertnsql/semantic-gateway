"""
models/semantic.py — Internal semantic domain models.

These Pydantic v2 models represent the gateway's understanding of the
dbt MetricFlow semantic layer.  They are populated at startup from YAML
files and the manifest, then used as the single source of truth by all
core services.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MetricDefinition(BaseModel):
    """
    Fully-enriched definition of a certified metric.

    Combines data from:
      - metrics/<name>.yml          → name, label, description, type, filter
      - models/semantic/sem_*.yml   → grain, dimensions, measures, joins
      - manifest.json               → lineage
    """

    name: str = Field(description="Canonical snake_case metric name.")
    label: str = Field(description="Human-readable display label.")
    description: str = Field(description="Business definition of the metric.")
    metric_type: str = Field(
        description="MetricFlow type: simple | ratio | derived."
    )
    source_model: str = Field(
        description="The dbt mart model that backs this metric, e.g. fct_mrr_monthly."
    )
    grain: str = Field(
        description="Human-readable description of the metric grain."
    )
    grain_columns: list[str] = Field(
        default_factory=list,
        description="Actual PK columns that define the grain of the source model.",
    )
    certified_dimensions: list[str] = Field(
        default_factory=list,
        description="Dimension names certified for use with this metric.",
    )
    time_dimension: str = Field(
        default="",
        description="Primary time dimension column for this metric.",
    )
    allowed_joins: list[str] = Field(
        default_factory=list,
        description="Other metric names that are safe to join with this metric.",
    )
    fanout_risk_models: list[str] = Field(
        default_factory=list,
        description="Source models that would cause a fanout if joined to this metric.",
    )
    measure_column: str = Field(
        default="",
        description="The underlying measure/column being aggregated.",
    )
    filter_expression: str | None = Field(
        default=None,
        description="MetricFlow filter expression applied to this metric, if any.",
    )
    valid_time_grains: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Map of time dimension to valid grains (e.g. {'period_month': ['month']}).",
    )
    lineage: list[str] = Field(
        default_factory=list,
        description="Ordered list of upstream model names from manifest.json.",
    )
    raw_yaml: dict = Field(
        default_factory=dict,
        description="The original parsed YAML block for reference.",
    )


class SemanticModelDefinition(BaseModel):
    """
    Parsed representation of a dbt MetricFlow semantic_model YAML block.
    Used internally during registry construction.
    """

    name: str
    description: str = ""
    model_ref: str = Field(description="The dbt model referenced, e.g. fct_mrr_monthly.")
    primary_entity: str = ""
    primary_entity_expr: str = ""
    dimensions: list[str] = Field(default_factory=list)
    time_dimensions: list[str] = Field(default_factory=list)
    time_dimensions_grains: dict[str, list[str]] = Field(default_factory=dict)
    measures: list[str] = Field(default_factory=list)
    grain_description: str = ""
    grain_columns: list[str] = Field(default_factory=list)
    raw_yaml: dict = Field(default_factory=dict)
