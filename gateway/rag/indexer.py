"""
gateway/rag/indexer.py — Standalone script to build the ChromaDB metric index.

Loads all metric definitions from the certified registry and embeds them
into a persistent ChromaDB collection so the gateway can do RAG retrieval.

Run as (from the ``gateway/`` directory)::

    python -m rag.indexer

This must be re-run whenever metrics YAML files change.
"""

from __future__ import annotations

import sys
import os

# Save downloaded HuggingFace models locally so Render preserves them
# between the build phase and the runtime phase.
os.environ["HF_HOME"] = os.path.abspath("./.hf_cache")

# Ensure the gateway package root is on sys.path when run via -m
_GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _GATEWAY_ROOT not in sys.path:
    sys.path.insert(0, _GATEWAY_ROOT)


def main() -> None:
    """Load the metric registry and index all metrics into ChromaDB."""
    from config import settings
    from core.manifest_parser import ManifestParser
    from core.metric_registry import MetricRegistry
    from rag.embedder import MetricEmbedder

    # ── 1. Parse the dbt manifest (best-effort; may not exist in dev) ─────────
    manifest_parser = ManifestParser()
    try:
        manifest_parser.load(settings.manifest_path)
        print(f"[indexer] Manifest loaded from '{settings.manifest_path}'.")
    except Exception as exc:
        print(f"[indexer] WARNING: Could not load manifest ({exc}). Lineage will be empty.")

    # ── 2. Load the metric registry ───────────────────────────────────────────
    registry = MetricRegistry()
    try:
        registry.load(
            settings.metrics_path,
            settings.semantic_models_path,
            manifest_parser,
        )
    except Exception as exc:
        print(f"[indexer] ERROR: Failed to load metrics: {exc}")
        sys.exit(1)

    all_metrics = registry.get_all_metrics()
    if not all_metrics:
        print("[indexer] ERROR: No metrics found in registry. Check METRICS_PATH and SEMANTIC_MODELS_PATH.")
        sys.exit(1)

    print(f"[indexer] Found {len(all_metrics)} metrics to index.")

    # ── 3. Index into ChromaDB ────────────────────────────────────────────────
    embedder = MetricEmbedder(persist_dir="./chroma_store")
    embedder.index_metrics(all_metrics)
    print(f"Indexed {len(all_metrics)} metrics into ChromaDB.")


if __name__ == "__main__":
    main()
