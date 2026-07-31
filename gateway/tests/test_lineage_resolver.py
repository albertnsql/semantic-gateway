"""
tests/test_lineage_resolver.py — Unit tests for LineageResolver.

This module had NO test coverage, which is how two display bugs reached the
Lineage Explorer at once:

  * Every raw source was listed TWICE in two different formats —
    "raw.subscribers" (from the manifest) and "subscribers" (synthesized from
    get_source_tables()). The synthesizing block predated sources being linked in
    the manifest and became duplicative once get_upstream_models() followed
    `source.` nodes; it is now a fallback for genuinely unlinked models.
  * The rendered chain read staging → intermediate → raw → marts, because
    get_upstream_models() emits sources first and resolve_model() reverses the
    list. Step order is now sorted by dbt layer explicitly.

Lineage is the trust surface of this product — it is what tells a user the number
came from certified models — so it is worth pinning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.lineage_resolver import _LAYER_ORDER, LineageResolver
from core.manifest_parser import ManifestParser
from core.metric_registry import MetricRegistry

_DBT_ROOT = (
    Path(__file__).parent.parent.parent / "dbt_streaming_analytics" / "streaming_analytics"
)
MANIFEST_PATH = str(_DBT_ROOT / "target" / "manifest.json")
METRICS_PATH = str(_DBT_ROOT / "metrics")
SEMANTIC_PATH = str(_DBT_ROOT / "models" / "semantic")

# Spread across every semantic model: mart-backed, ratio, and staging-backed.
METRICS = [
    "mrr",
    "churn_rate",
    "monthly_churned_subscribers",
    "ltv",
    "total_sessions",
    "clicked_recommendations",
]


@pytest.fixture(scope="module")
def resolver() -> LineageResolver:
    if not Path(MANIFEST_PATH).exists():
        pytest.skip("Real manifest.json not available — run `dbt compile`.")
    parser = ManifestParser()
    parser.load(MANIFEST_PATH)
    registry = MetricRegistry()
    registry.load(METRICS_PATH, SEMANTIC_PATH, parser)
    return LineageResolver(parser, registry)


class TestSourceTables:
    @pytest.mark.parametrize("metric", METRICS)
    def test_no_duplicate_source_tables(self, resolver, metric) -> None:
        srcs = resolver.resolve_metric(metric).source_tables
        assert len(srcs) == len(set(srcs)), f"duplicates: {srcs}"

    @pytest.mark.parametrize("metric", METRICS)
    def test_source_tables_use_one_format(self, resolver, metric) -> None:
        """The regression: both 'raw.subscribers' and bare 'subscribers' appeared."""
        srcs = resolver.resolve_metric(metric).source_tables
        assert srcs, f"{metric} resolved no source tables"
        unqualified = [s for s in srcs if "." not in s]
        assert not unqualified, f"bare table names alongside qualified ones: {unqualified}"

    @pytest.mark.parametrize("metric", METRICS)
    def test_no_source_appears_in_both_forms(self, resolver, metric) -> None:
        srcs = resolver.resolve_metric(metric).source_tables
        bare = {s.split(".")[-1] for s in srcs}
        assert len(bare) == len(srcs), f"same table listed twice: {sorted(srcs)}"


class TestStepOrdering:
    @pytest.mark.parametrize("metric", METRICS)
    def test_steps_follow_dbt_layer_order(self, resolver, metric) -> None:
        """dbt lineage flows one direction: raw → staging → intermediate → marts."""
        steps = resolver.resolve_metric(metric).transformation_steps
        ranks = [_LAYER_ORDER.get(s.layer, len(_LAYER_ORDER)) for s in steps]
        assert ranks == sorted(ranks), (
            "layers out of order: "
            + " → ".join(f"{s.layer}:{s.model_name}" for s in steps)
        )

    @pytest.mark.parametrize("metric", METRICS)
    def test_no_duplicate_steps(self, resolver, metric) -> None:
        names = [s.model_name for s in resolver.resolve_metric(metric).transformation_steps]
        assert len(names) == len(set(names)), f"duplicate steps: {names}"

    @pytest.mark.parametrize("metric", METRICS)
    def test_chain_starts_at_raw(self, resolver, metric) -> None:
        steps = resolver.resolve_metric(metric).transformation_steps
        assert steps[0].layer == "raw", f"chain starts at {steps[0].layer}"

    @pytest.mark.parametrize("metric", METRICS)
    def test_chain_ends_at_the_metrics_source_model(self, resolver, metric) -> None:
        trace = resolver.resolve_metric(metric)
        assert trace.transformation_steps[-1].model_name == trace.source_model


class TestStagingBackedMetrics:
    """
    sem_recommendation_events maps to a STAGING model, not a mart, unlike every
    other semantic model. Following only `model.` dependencies gave it an empty
    chain, so it fell back to hardcoded values.
    """

    def test_staging_backed_metric_resolves_from_the_manifest(self, resolver) -> None:
        trace = resolver.resolve_metric("clicked_recommendations")
        assert trace.source_tables == ["raw.recommendation_events"]
        assert [s.model_name for s in trace.transformation_steps] == [
            "raw.recommendation_events",
            "stg_recommendation_events",
        ]


class TestFallbackStillWorks:
    def test_unlinked_model_falls_back_to_inference(self, resolver) -> None:
        """A model with no manifest sources must still report something."""
        trace = resolver.resolve_model("does_not_exist_anywhere")
        assert trace.source_tables, "fallback produced no source tables"
