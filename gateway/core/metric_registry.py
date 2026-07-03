"""
core/metric_registry.py — Queryable in-memory registry of certified metrics.

Single responsibility: build MetricDefinition objects from YAML files and
expose a clean, case-insensitive lookup API.  No SQL, no HTTP calls.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from core.exceptions import MetricNotFoundError, MetricsLoadError
from models.semantic import MetricDefinition, SemanticModelDefinition

if TYPE_CHECKING:
    from core.manifest_parser import ManifestParser

logger = logging.getLogger(__name__)


# Mapping: semantic model name → source dbt model (fct_ / dim_)
_SEMANTIC_TO_MODEL: dict[str, str] = {
    "sem_mrr": "fct_mrr_monthly",
    "sem_payments": "fct_payments",
    "sem_stream_sessions": "fct_stream_sessions",
    "sem_subscribers": "dim_subscribers",
    "sem_recommendation_events": "stg_recommendation_events",
}

# Fanout risk: which pairs of source models cannot be safely joined
# (subscription grain vs session grain — fanout guaranteed)
_FANOUT_PAIRS: set[frozenset[str]] = {
    frozenset({"fct_mrr_monthly", "fct_stream_sessions"}),
    frozenset({"fct_mrr_monthly", "stg_recommendation_events"}),
}

# Allowed cross-metric joins (same grain or safe via bridge)
_ALLOWED_JOINS: dict[str, list[str]] = {
    "mrr": ["expansion_mrr", "churn_rate", "total_subscribers"],
    "expansion_mrr": ["mrr", "churn_rate"],
    "churn_rate": ["mrr", "total_subscribers"],
    "total_subscribers": ["churn_rate", "mrr"],
    "ltv": ["total_subscribers"],
    "engagement_rate": ["recommendation_ctr"],
    "recommendation_ctr": ["engagement_rate"],
}


def _extract_model_name_from_ref(ref_str: str) -> str:
    """Parse ``ref('fct_mrr_monthly')`` → ``fct_mrr_monthly``."""
    ref_str = ref_str.strip()
    if "ref(" in ref_str:
        inner = ref_str.split("ref(")[1].split(")")[0]
        return inner.strip("'\"")
    return ref_str


class MetricRegistry:
    """
    Loads all metric YAML files and semantic model YAML files.
    Builds a queryable in-memory registry of :class:`MetricDefinition` objects.

    Usage::

        registry = MetricRegistry()
        registry.load(metrics_path, semantic_path, manifest_parser)
        metric = registry.get_metric("mrr")
    """

    def __init__(self) -> None:
        self._metrics: dict[str, MetricDefinition] = {}  # lower-cased name → def
        self._semantic_models: dict[str, SemanticModelDefinition] = {}
        self._loaded: bool = False

    # ------------------------------------------------------------------ public

    def load(
        self,
        metrics_path: str,
        semantic_path: str,
        manifest_parser: "ManifestParser",
    ) -> None:
        """
        Read all .yml files from metrics/ and models/semantic/.
        Build MetricDefinition objects enriched with lineage from manifest_parser.

        Args:
            metrics_path: Directory containing metric YAML files.
            semantic_path: Directory containing semantic model YAML files.
            manifest_parser: Already-loaded ManifestParser instance.

        Raises:
            MetricsLoadError: If any YAML file is malformed.
        """
        sem_models = self._load_semantic_models(semantic_path)
        self._semantic_models = sem_models

        raw_metrics = self._load_raw_metrics(metrics_path)

        for raw in raw_metrics:
            name = raw.get("name", "")
            if not name:
                continue

            sem = self._find_semantic_for_metric(name, sem_models)
            source_model = sem.model_ref if sem else ""

            # Lineage from manifest
            lineage: list[str] = []
            if source_model:
                try:
                    lineage = manifest_parser.get_upstream_models(source_model)
                except Exception:
                    pass

            metric_type = raw.get("type", "simple")

            # Determine measure column
            type_params = raw.get("type_params", {})
            measure_col = ""
            if metric_type == "simple":
                measure_col = type_params.get("measure", "")
            elif metric_type == "ratio":
                measure_col = (
                    f"{type_params.get('numerator', '')} / "
                    f"{type_params.get('denominator', '')}"
                )
            elif metric_type == "derived":
                measure_col = str(type_params.get("expr", ""))

            defn = MetricDefinition(
                name=name,
                label=raw.get("label", name.replace("_", " ").title()),
                description=raw.get("description", ""),
                metric_type=metric_type,
                source_model=source_model,
                grain=sem.grain_description if sem else "",
                grain_columns=sem.grain_columns if sem else [],
                certified_dimensions=(sem.dimensions + sem.time_dimensions) if sem else [],
                time_dimension=sem.time_dimensions[0] if sem and sem.time_dimensions else "",
                valid_time_grains=sem.time_dimensions_grains if sem else {},
                allowed_joins=_ALLOWED_JOINS.get(name, []),
                fanout_risk_models=self._get_fanout_risk_models(source_model),
                measure_column=measure_col,
                filter_expression=raw.get("filter", None),
                lineage=lineage,
                raw_yaml=raw,
            )
            self._metrics[name.lower()] = defn

        self._loaded = True

        # Second pass: enrich certified_dimensions for specific metrics
        # based on known cross-entity foreign joins
        for m_name, m_def in self._metrics.items():
            native_count = len(m_def.certified_dimensions)
            
            if m_name == "ltv":
                sub_sem = sem_models.get("sem_subscribers")
                if sub_sem:
                    m_def.certified_dimensions.extend(sub_sem.dimensions + sub_sem.time_dimensions)
            elif m_name in (
                "engagement_rate", "recommendation_ctr", 
                "total_recommendations", "clicked_recommendations",
                "avg_watch_time", "total_watch_time",
                "avg_buffering_events", "total_buffering_events", "total_sessions"
            ):
                sub_sem = sem_models.get("sem_subscribers")
                if sub_sem:
                    m_def.certified_dimensions.extend(sub_sem.dimensions + sub_sem.time_dimensions)
                m_def.certified_dimensions.extend(["content_type", "primary_genre", "is_original"])
                
            # Deduplicate while preserving order
            seen = set()
            m_def.certified_dimensions = [x for x in m_def.certified_dimensions if not (x in seen or seen.add(x))]
            
            after_count = len(m_def.certified_dimensions)
            logger.info("Registry Metric: '%s' | Native dims: %d | After joins: %d", m_name, native_count, after_count)

        logger.info(
            "MetricRegistry loaded: %d certified metrics, %d semantic models.",
            len(self._metrics),
            len(sem_models),
        )

    def get_metric(self, name: str) -> MetricDefinition | None:
        """
        Return a MetricDefinition by name (case-insensitive).

        Args:
            name: Metric name, e.g. ``"MRR"`` or ``"mrr"``.

        Returns:
            The :class:`MetricDefinition`, or ``None`` if not found.
        """
        return self._metrics.get(name.lower())

    def list_metrics(self) -> list[MetricDefinition]:
        """Return all certified metrics."""
        return list(self._metrics.values())

    def get_all_metrics(self) -> list[dict]:
        """Return raw YAML dicts for all certified metrics (used by the RAG indexer)."""
        results: list[dict] = []
        for defn in self._metrics.values():
            raw = dict(defn.raw_yaml) if defn.raw_yaml else {"name": defn.name}
            # Augment with resolved dimension list so the embedder has accurate data
            raw.setdefault("dimensions", defn.certified_dimensions)
            raw.setdefault("time_dimensions", defn.valid_time_grains)
            results.append(raw)
        return results

    def get_dimensions_for_metric(self, metric_name: str) -> list[str]:
        """
        Return all certified dimensions for a given metric.

        Args:
            metric_name: Metric name (case-insensitive).

        Returns:
            List of certified dimension names.

        Raises:
            MetricNotFoundError: If the metric is not in the registry.
        """
        metric = self.get_metric(metric_name)
        if metric is None:
            raise MetricNotFoundError(metric_name)
        return metric.certified_dimensions

    def get_valid_time_grains_for_metric(self, metric_name: str) -> dict[str, list[str]]:
        """Return valid time grains for a metric's time dimensions."""
        metric = self.get_metric(metric_name)
        return metric.valid_time_grains if metric else {}

    def is_certified_metric(self, name: str) -> bool:
        """Return True if the named metric exists in the registry."""
        return name.lower() in self._metrics

    def is_certified_dimension(self, metric_name: str, dimension: str) -> bool:
        """
        Return True if *dimension* is a certified dimension for *metric_name*.

        Args:
            metric_name: Metric name (case-insensitive).
            dimension: Dimension name (case-insensitive).

        Returns:
            ``True`` if certified; ``False`` if metric not found or dim uncertified.
        """
        metric = self.get_metric(metric_name)
        if metric is None:
            return False
        return dimension.lower() in [d.lower() for d in metric.certified_dimensions]

    def get_grain(self, metric_name: str) -> str:
        """Return human-readable grain description for a metric."""
        metric = self.get_metric(metric_name)
        return metric.grain if metric else ""

    def get_grain_columns(self, metric_name: str) -> list[str]:
        """Return the PK columns that define the metric's grain."""
        metric = self.get_metric(metric_name)
        return metric.grain_columns if metric else []

    def would_cause_fanout(self, metric_a: str, metric_b: str) -> bool:
        """
        Return True if joining metric_a with metric_b would cause a row
        multiplication fanout.

        Logic: check if the source models of both metrics form a known
        incompatible pair (different grains with no bridging entity).

        Args:
            metric_a: First metric name.
            metric_b: Second metric name.

        Returns:
            ``True`` if the join is unsafe.
        """
        m_a = self.get_metric(metric_a)
        m_b = self.get_metric(metric_b)
        if m_a is None or m_b is None:
            return False

        pair = frozenset({m_a.source_model, m_b.source_model})
        if pair in _FANOUT_PAIRS:
            return True

        # Also check if metric_b's source is in metric_a's fanout_risk_models
        if m_b.source_model in m_a.fanout_risk_models:
            return True
        if m_a.source_model in m_b.fanout_risk_models:
            return True

        return False

    def get_source_model(self, metric_name: str) -> str:
        """Return the mart model backing a metric."""
        metric = self.get_metric(metric_name)
        return metric.source_model if metric else ""

    def get_all_dimension_map(self) -> dict[str, list[str]]:
        """
        Return ``{metric_name: [certified_dimensions]}`` for all metrics.
        Used by IntentExtractor to build its system prompt.
        """
        return {name: m.certified_dimensions for name, m in self._metrics.items()}

    def count_semantic_models(self) -> int:
        """Return the number of loaded semantic models."""
        return len(self._semantic_models)

    # ----------------------------------------------------------------- private

    def _load_semantic_models(
        self, semantic_path: str
    ) -> dict[str, SemanticModelDefinition]:
        """Parse all YAML files in semantic_path and return a name→SemanticModelDef map."""
        results: dict[str, SemanticModelDefinition] = {}
        p = Path(semantic_path)
        if not p.exists():
            logger.warning("Semantic models path '%s' not found — skipping.", p)
            return results

        for yml_file in sorted(p.glob("*.yml")):
            try:
                with yml_file.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
            except Exception as exc:
                raise MetricsLoadError(
                    f"Failed to parse semantic model YAML '{yml_file}': {exc}"
                ) from exc

            for sm in data.get("semantic_models", []):
                parsed = self._parse_semantic_model(sm)
                results[parsed.name] = parsed

        logger.debug("Loaded %d semantic models from '%s'.", len(results), p)
        return results

    def _parse_semantic_model(self, sm: dict) -> SemanticModelDefinition:
        """Convert a raw semantic model YAML dict into a SemanticModelDefinition."""
        model_ref_raw = sm.get("model", "")
        model_ref = _extract_model_name_from_ref(model_ref_raw)

        entities = sm.get("entities", [])
        primary_entity = ""
        primary_entity_expr = ""
        grain_columns: list[str] = []
        for e in entities:
            if e.get("type") == "primary":
                primary_entity = e.get("name", "")
                primary_entity_expr = e.get("expr", "")
                grain_columns = [primary_entity_expr] if primary_entity_expr else []
                break

        dims_raw = sm.get("dimensions", [])
        categoricals: list[str] = []
        time_dims: list[str] = []
        time_dimensions_grains: dict[str, list[str]] = {}
        
        grain_hierarchy = ["day", "week", "month", "quarter", "year"]
        
        for d in dims_raw:
            dname = d.get("name", "")
            if d.get("type") == "time":
                time_dims.append(dname)
                grain = d.get("type_params", {}).get("time_granularity")
                if grain and grain.lower() in grain_hierarchy:
                    idx = grain_hierarchy.index(grain.lower())
                    time_dimensions_grains[dname] = grain_hierarchy[idx:]
                elif grain:
                    time_dimensions_grains[dname] = [grain]
                else:
                    time_dimensions_grains[dname] = grain_hierarchy
            else:
                categoricals.append(dname)

        measures = [m.get("name", "") for m in sm.get("measures", [])]

        # Build human-readable grain description
        grain_desc = (
            f"One row per {primary_entity} (keyed on {primary_entity_expr})"
            if primary_entity_expr
            else sm.get("description", "")
        )

        return SemanticModelDefinition(
            name=sm.get("name", ""),
            description=sm.get("description", ""),
            model_ref=model_ref,
            primary_entity=primary_entity,
            primary_entity_expr=primary_entity_expr,
            dimensions=categoricals,
            time_dimensions=time_dims,
            time_dimensions_grains=time_dimensions_grains,
            measures=measures,
            grain_description=grain_desc,
            grain_columns=grain_columns,
            raw_yaml=sm,
        )

    def _load_raw_metrics(self, metrics_path: str) -> list[dict]:
        """Parse all metric YAML files and return a flat list of metric dicts."""
        results: list[dict] = []
        p = Path(metrics_path)
        if not p.exists():
            raise MetricsLoadError(
                f"Metrics path '{p.resolve()}' does not exist."
            )

        for yml_file in sorted(p.glob("*.yml")):
            try:
                with yml_file.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
            except Exception as exc:
                raise MetricsLoadError(
                    f"Failed to parse metric YAML '{yml_file}': {exc}"
                ) from exc

            for m in data.get("metrics", []):
                results.append(m)

        logger.debug("Loaded %d raw metric definitions from '%s'.", len(results), p)
        return results

    def _find_semantic_for_metric(
        self,
        metric_name: str,
        sem_models: dict[str, SemanticModelDefinition],
    ) -> SemanticModelDefinition | None:
        """
        Find the semantic model whose measures contain a measure matching the
        metric's type_params (by name or by model reference convention).
        """
        # Heuristic mapping based on this project's naming conventions
        _METRIC_TO_SEM: dict[str, str] = {
            "mrr": "sem_mrr",
            "expansion_mrr": "sem_mrr",
            "total_revenue": "sem_mrr",
            "net_mrr_growth": "sem_mrr",
            "ltv": "sem_payments",
            "engagement_rate": "sem_stream_sessions",
            "avg_watch_time": "sem_stream_sessions",
            "total_watch_time": "sem_stream_sessions",
            "avg_buffering_events": "sem_stream_sessions",
            "total_buffering_events": "sem_stream_sessions",
            "total_sessions": "sem_stream_sessions",
            "churn_rate": "sem_subscribers",
            "retention_rate": "sem_subscribers",
            "total_subscribers": "sem_subscribers",
            "churned_subscribers": "sem_subscribers",
            "recommendation_ctr": "sem_recommendation_events",
            "clicked_recommendations": "sem_recommendation_events",
            "total_recommendations": "sem_recommendation_events",
        }
        sem_name = _METRIC_TO_SEM.get(metric_name.lower())
        if sem_name:
            return sem_models.get(sem_name)
        # Fallback: search by measure name
        for sm in sem_models.values():
            if metric_name.lower() in [m.lower() for m in sm.measures]:
                return sm
        return None

    def _get_fanout_risk_models(self, source_model: str) -> list[str]:
        """Return the list of models that would fanout if joined to source_model."""
        risks: list[str] = []
        for pair in _FANOUT_PAIRS:
            if source_model in pair:
                risks.extend(pair - {source_model})
        return risks
