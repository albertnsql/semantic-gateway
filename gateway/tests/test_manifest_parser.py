"""
tests/test_manifest_parser.py — Unit tests for ManifestParser.

Tests use the real manifest.json from the streaming_analytics dbt project.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from core.exceptions import ManifestLoadError
from core.manifest_parser import ManifestParser

# Path to the actual manifest.json
MANIFEST_PATH = str(
    Path(__file__).parent.parent.parent
    / "dbt_streaming_analytics"
    / "streaming_analytics"
    / "target"
    / "manifest.json"
)


@pytest.fixture
def parser() -> ManifestParser:
    """Return a ManifestParser loaded with the real manifest."""
    p = ManifestParser()
    if Path(MANIFEST_PATH).exists():
        p.load(MANIFEST_PATH)
    return p


@pytest.fixture
def minimal_manifest(tmp_path: Path) -> str:
    """Create a minimal manifest.json fixture for isolated tests."""
    data = {
        "metadata": {"dbt_version": "1.11.0"},
        "nodes": {
            "model.streaming_analytics.fct_mrr_monthly": {
                "name": "fct_mrr_monthly",
                "resource_type": "model",
                "schema": "marts",
                "description": "Monthly MRR per subscription",
                "columns": {
                    "subscription_id": {"name": "subscription_id"},
                    "mrr_usd": {"name": "mrr_usd"},
                    "period_month": {"name": "period_month"},
                },
                "depends_on": {
                    "nodes": [
                        "model.streaming_analytics.int_subscription_periods",
                    ]
                },
            },
            "model.streaming_analytics.int_subscription_periods": {
                "name": "int_subscription_periods",
                "resource_type": "model",
                "schema": "intermediate",
                "description": "One row per subscription per month",
                "columns": {},
                "depends_on": {
                    "nodes": [
                        "model.streaming_analytics.stg_subscriptions",
                    ]
                },
            },
            "model.streaming_analytics.stg_subscriptions": {
                "name": "stg_subscriptions",
                "resource_type": "model",
                "schema": "staging",
                "description": "Staged subscriptions",
                "columns": {},
                "depends_on": {"nodes": []},
            },
        },
        "sources": {
            "source.streaming_analytics.raw.subscriptions": {
                "name": "subscriptions",
                "schema": "raw",
            }
        },
    }
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(data))
    return str(manifest_file)


class TestManifestParserLoad:
    def test_load_valid_manifest(self, minimal_manifest: str) -> None:
        """Loading a valid manifest should succeed and report model count > 0."""
        p = ManifestParser()
        p.load(minimal_manifest)
        assert p._loaded is True
        models = p.get_all_models()
        assert len(models) > 0

    def test_load_missing_file_raises(self) -> None:
        """Loading a non-existent path should raise ManifestLoadError."""
        p = ManifestParser()
        with pytest.raises(ManifestLoadError, match="not found"):
            p.load("/nonexistent/path/manifest.json")

    def test_load_invalid_json_raises(self, tmp_path: Path) -> None:
        """Loading a corrupt JSON file should raise ManifestLoadError."""
        bad_file = tmp_path / "manifest.json"
        bad_file.write_text("{invalid json")
        p = ManifestParser()
        with pytest.raises(ManifestLoadError, match="not valid JSON"):
            p.load(str(bad_file))

    def test_assert_loaded_before_use(self) -> None:
        """Calling methods before load() should raise ManifestLoadError."""
        p = ManifestParser()
        with pytest.raises(ManifestLoadError, match="load\\(\\) has not been called"):
            p.get_all_models()


class TestManifestParserGetModelNode:
    def test_get_model_node_known(self, minimal_manifest: str) -> None:
        """Known model should return a non-empty dict."""
        p = ManifestParser()
        p.load(minimal_manifest)
        node = p.get_model_node("fct_mrr_monthly")
        assert isinstance(node, dict)
        assert node.get("name") == "fct_mrr_monthly"

    def test_get_model_node_unknown_returns_empty(self, minimal_manifest: str) -> None:
        """Unknown model should return an empty dict (not raise)."""
        p = ManifestParser()
        p.load(minimal_manifest)
        assert p.get_model_node("nonexistent_model") == {}


class TestManifestParserGetColumns:
    def test_get_model_columns(self, minimal_manifest: str) -> None:
        """fct_mrr_monthly should have the documented columns."""
        p = ManifestParser()
        p.load(minimal_manifest)
        cols = p.get_model_columns("fct_mrr_monthly")
        assert "subscription_id" in cols
        assert "mrr_usd" in cols

    def test_get_model_columns_unknown_returns_empty(self, minimal_manifest: str) -> None:
        """Unknown model should return empty list."""
        p = ManifestParser()
        p.load(minimal_manifest)
        assert p.get_model_columns("unknown") == []


class TestManifestParserUpstreamModels:
    def test_get_upstream_models_fct_mrr(self, minimal_manifest: str) -> None:
        """fct_mrr_monthly should have int_subscription_periods in its upstream chain."""
        p = ManifestParser()
        p.load(minimal_manifest)
        upstream = p.get_upstream_models("fct_mrr_monthly")
        assert "int_subscription_periods" in upstream

    def test_get_upstream_models_leaf_node(self, minimal_manifest: str) -> None:
        """stg_subscriptions has no upstream models."""
        p = ManifestParser()
        p.load(minimal_manifest)
        upstream = p.get_upstream_models("stg_subscriptions")
        assert upstream == []

    def test_get_upstream_models_real_manifest(self) -> None:
        """If real manifest exists, fct_mrr_monthly must have int_subscription_periods upstream."""
        if not Path(MANIFEST_PATH).exists():
            pytest.skip("Real manifest.json not available.")
        p = ManifestParser()
        p.load(MANIFEST_PATH)
        upstream = p.get_upstream_models("fct_mrr_monthly")
        assert "int_subscription_periods" in upstream


class TestManifestParserLineageGraph:
    def test_build_lineage_graph_returns_dict(self, minimal_manifest: str) -> None:
        """build_lineage_graph should return a dict."""
        p = ManifestParser()
        p.load(minimal_manifest)
        graph = p.build_lineage_graph()
        assert isinstance(graph, dict)

    def test_build_lineage_graph_no_self_loops(self, minimal_manifest: str) -> None:
        """No model should list itself as a dependency (no self-loops)."""
        p = ManifestParser()
        p.load(minimal_manifest)
        graph = p.build_lineage_graph()
        for model, deps in graph.items():
            assert model not in deps, f"Self-loop detected on model '{model}'"

    def test_real_manifest_model_count(self) -> None:
        """Real manifest should have more than 10 models."""
        if not Path(MANIFEST_PATH).exists():
            pytest.skip("Real manifest.json not available.")
        p = ManifestParser()
        p.load(MANIFEST_PATH)
        models = p.get_all_models()
        assert len(models) > 10
