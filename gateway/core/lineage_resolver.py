"""
core/lineage_resolver.py — Upstream lineage resolution for metrics and models.

Single responsibility: walk the dbt manifest dependency graph and assemble
a human-readable LineageTrace describing the transformation path from raw
source tables to the certified metric.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from core.exceptions import MetricNotFoundError

if TYPE_CHECKING:
    from core.manifest_parser import ManifestParser
    from core.metric_registry import MetricRegistry

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────── Data models

class TransformationStep(BaseModel):
    """A single model in the transformation chain."""

    model_name: str
    layer: str  # raw | staging | intermediate | marts
    description: str
    columns_used: list[str]


class LineageTrace(BaseModel):
    """
    Full upstream lineage trace for a metric or model.

    Populated by :class:`LineageResolver` and included in the governance
    block of every successful GatewayResponse.
    """

    metric_name: str
    source_model: str
    upstream_models: list[str]
    source_tables: list[str]
    transformation_steps: list[TransformationStep]


# ──────────────────────────────────────────────── Layer classification

_LAYER_KEYWORDS: dict[str, str] = {
    "raw": "raw",
    "source": "raw",
    "stg": "staging",
    "staging": "staging",
    "int": "intermediate",
    "intermediate": "intermediate",
    "fct": "marts",
    "dim": "marts",
    "marts": "marts",
    "mart": "marts",
}


def _classify_layer(model_name: str, schema: str = "") -> str:
    """
    Classify a model name into a dbt layer bucket.

    Classification uses a combination of model name prefix and schema name.

    Args:
        model_name: Short model name, e.g. ``"stg_payments"``.
        schema: Snowflake schema name from manifest, e.g. ``"staging"``.

    Returns:
        One of ``raw``, ``staging``, ``intermediate``, ``marts``.
    """
    schema_lower = schema.lower()
    for kw, layer in _LAYER_KEYWORDS.items():
        if schema_lower.startswith(kw) or schema_lower == kw:
            return layer

    name_lower = model_name.lower()
    for kw, layer in _LAYER_KEYWORDS.items():
        if name_lower.startswith(kw + "_") or name_lower.startswith(kw):
            return layer

    return "marts"


class LineageResolver:
    """
    Resolves upstream lineage for a metric or model by walking the
    ManifestParser dependency graph.

    Builds a :class:`LineageTrace` that shows the full data journey from
    raw source tables through staging, intermediate, and mart layers.

    Usage::

        resolver = LineageResolver(manifest_parser, metric_registry)
        trace = resolver.resolve_metric("mrr")
        print(trace.transformation_steps)
    """

    def __init__(
        self,
        manifest_parser: "ManifestParser",
        metric_registry: "MetricRegistry",
    ) -> None:
        self._manifest = manifest_parser
        self._registry = metric_registry

    # ──────────────────────────────────────────────── public

    def resolve_metric(self, metric_name: str) -> LineageTrace:
        """
        Build a complete LineageTrace for a certified metric.

        Steps:
          1. Get source_model from MetricRegistry
          2. Walk upstream via ManifestParser.get_upstream_models()
          3. Classify each model by layer
          4. Build TransformationStep list in dependency order
          5. Identify raw source tables at the leaves

        Args:
            metric_name: Certified metric name (case-insensitive).

        Returns:
            :class:`LineageTrace` for the metric.

        Raises:
            MetricNotFoundError: If the metric is not in the registry.
        """
        metric = self._registry.get_metric(metric_name)
        if metric is None:
            raise MetricNotFoundError(metric_name)

        return self.resolve_model(metric.source_model, metric_name=metric_name)

    def resolve_model(
        self, model_name: str, metric_name: str = ""
    ) -> LineageTrace:
        """
        Build a LineageTrace starting from a model name directly.

        Args:
            model_name: Short dbt model name, e.g. ``"fct_mrr_monthly"``.
            metric_name: Optional metric name to attach to the trace.

        Returns:
            :class:`LineageTrace` for the model.
        """
        upstream_models = self._manifest.get_upstream_models(model_name)

        # Build steps: start from the deepest ancestor up to the source model
        all_models_in_order = list(reversed(upstream_models)) + [model_name]

        steps: list[TransformationStep] = []
        source_tables: list[str] = []

        for m in all_models_in_order:
            schema = self._manifest.get_model_schema(m)
            layer = _classify_layer(m, schema)
            description = self._manifest.get_model_description(m) or ""
            columns = self._manifest.get_model_columns(m)

            if layer == "raw":
                source_tables.append(m)

            steps.append(
                TransformationStep(
                    model_name=m,
                    layer=layer,
                    description=description,
                    columns_used=columns[:10],  # cap for brevity
                )
            )

        # Also pull manifest source tables (leaves)
        source_nodes = self._manifest.get_source_tables()
        for sn in source_nodes:
            # Format: "source.streaming_analytics.raw.subscribers"
            parts = sn.split(".")
            if len(parts) >= 3:
                table_name = parts[-1]
                if table_name not in source_tables and any(
                    table_name in m for m in upstream_models
                ):
                    source_tables.append(table_name)

        logger.debug(
            "Resolved lineage for '%s': %d steps, %d source tables.",
            model_name,
            len(steps),
            len(source_tables),
        )

        return LineageTrace(
            metric_name=metric_name or model_name,
            source_model=model_name,
            upstream_models=upstream_models,
            source_tables=source_tables or self._infer_source_tables(model_name),
            transformation_steps=steps,
        )

    def get_certified_definition(self, metric_name: str) -> str:
        """
        Return a human-readable certified definition string combining the
        metric description, grain, and lineage path.

        Format::

            "MRR is defined as [description]. Grain: [grain]. Certified source:
             [source_model]. Lineage: raw.subscriptions → stg_subscriptions →
             int_subscription_periods → fct_mrr_monthly."

        Args:
            metric_name: Certified metric name (case-insensitive).

        Returns:
            Human-readable certified definition string.

        Raises:
            MetricNotFoundError: If the metric is not in the registry.
        """
        metric = self._registry.get_metric(metric_name)
        if metric is None:
            raise MetricNotFoundError(metric_name)

        try:
            trace = self.resolve_metric(metric_name)
            lineage_path = " → ".join(
                step.model_name for step in trace.transformation_steps
            )
        except Exception:
            lineage_path = metric.source_model

        return (
            f"{metric.label} is defined as: {metric.description}. "
            f"Grain: {metric.grain}. "
            f"Certified source: {metric.source_model}. "
            f"Lineage: {lineage_path}."
        )

    # ──────────────────────────────────────────────── private

    def _infer_source_tables(self, model_name: str) -> list[str]:
        """
        Infer likely raw source tables from model name when manifest
        sources are not linked.

        WARNING: This is a hardcoded fallback. If the manifest is not providing
        complete lineage, run 'dbt docs generate' to refresh it.
        New models added to the project will not be found here automatically.
        """
        _MODEL_SOURCES: dict[str, list[str]] = {
            "fct_mrr_monthly": ["raw.subscriptions"],
            "fct_payments": ["raw.payments"],
            "fct_stream_sessions": ["raw.stream_sessions"],
            "dim_subscribers": ["raw.subscribers"],
            "stg_recommendation_events": ["raw.recommendation_events"],
        }
        if model_name not in _MODEL_SOURCES:
            guessed = f"raw.{model_name.replace('fct_', '').replace('dim_', '').replace('stg_', '')}"
            logger.warning(
                "Lineage for '%s' could not be resolved from manifest — using guessed fallback '%s'. "
                "Run 'dbt docs generate' to refresh the manifest and get accurate lineage.",
                model_name,
                guessed,
            )
            return [guessed]
        logger.warning(
            "Lineage for '%s' resolved via hardcoded fallback (manifest incomplete). "
            "Run 'dbt docs generate' to refresh.",
            model_name,
        )
        return _MODEL_SOURCES[model_name]

