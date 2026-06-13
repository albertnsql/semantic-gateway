"""
core/manifest_parser.py — dbt manifest.json parser and in-memory index.

Single responsibility: load the dbt manifest once at startup, expose
read-only accessors that all downstream services can call without
touching the filesystem again.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from core.exceptions import ManifestLoadError

logger = logging.getLogger(__name__)

_MODEL_PREFIX = "model.streaming_analytics."
_SOURCE_PREFIX = "source.streaming_analytics."


class ManifestParser:
    """
    Parses the dbt manifest.json and builds an in-memory index of all model
    nodes, their columns, descriptions, and dependency edges.

    Usage::

        parser = ManifestParser()
        parser.load("../dbt/target/manifest.json")
        upstream = parser.get_upstream_models("fct_mrr_monthly")
    """

    def __init__(self) -> None:
        self._raw: dict[str, Any] = {}
        self._nodes: dict[str, Any] = {}
        self._sources: dict[str, Any] = {}
        self._loaded: bool = False

    # ------------------------------------------------------------------ public

    def load(self, manifest_path: str) -> None:
        """
        Load and parse the manifest.json file.

        Args:
            manifest_path: Absolute or relative path to manifest.json.

        Raises:
            ManifestLoadError: If the file is missing or not valid JSON.
        """
        path = Path(manifest_path)
        if not path.exists():
            raise ManifestLoadError(
                f"manifest.json not found at '{path.resolve()}'. "
                "Run 'dbt compile' or 'dbt run' first to generate the manifest."
            )

        try:
            with path.open("r", encoding="utf-8") as fh:
                self._raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ManifestLoadError(
                f"manifest.json at '{path}' is not valid JSON: {exc}"
            ) from exc

        self._nodes = self._raw.get("nodes", {})
        self._sources = self._raw.get("sources", {})
        self._loaded = True

        model_count = sum(
            1 for k in self._nodes if k.startswith(_MODEL_PREFIX)
        )
        source_count = len(self._sources)
        logger.info(
            "Manifest loaded: %d model nodes, %d source nodes from '%s'.",
            model_count,
            source_count,
            path,
        )

    def get_model_node(self, model_name: str) -> dict[str, Any]:
        """
        Return the full manifest node dict for a given model name.

        Args:
            model_name: Short name, e.g. ``"fct_mrr_monthly"``.

        Returns:
            The manifest node dict, or an empty dict if not found.
        """
        self._assert_loaded()
        key = f"{_MODEL_PREFIX}{model_name}"
        return self._nodes.get(key, {})

    def get_model_columns(self, model_name: str) -> list[str]:
        """
        Return the list of column names documented in the manifest for a model.

        Args:
            model_name: Short model name.

        Returns:
            List of column name strings (may be empty if no columns documented).
        """
        node = self.get_model_node(model_name)
        return list(node.get("columns", {}).keys())

    def get_upstream_models(self, model_name: str) -> list[str]:
        """
        Walk the depends_on.nodes graph recursively (BFS) and return all
        upstream model names in topological order (dependencies first).

        Args:
            model_name: Short name of the model to trace from.

        Returns:
            Ordered list of short upstream model names.
        """
        self._assert_loaded()
        visited: set[str] = set()
        result: list[str] = []
        queue: deque[str] = deque([model_name])

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)

            node_key = f"{_MODEL_PREFIX}{current}"
            node = self._nodes.get(node_key, {})
            deps = node.get("depends_on", {}).get("nodes", [])

            for dep in deps:
                if dep.startswith(_MODEL_PREFIX):
                    short = dep[len(_MODEL_PREFIX):]
                    if short not in visited:
                        result.append(short)
                        queue.append(short)

        return result

    def get_model_description(self, model_name: str) -> str:
        """
        Return the model's description from the manifest.

        Args:
            model_name: Short model name.

        Returns:
            Description string, or empty string if not present.
        """
        node = self.get_model_node(model_name)
        return node.get("description", "")

    def get_all_models(self) -> list[str]:
        """
        Return all model names present in the manifest.

        Returns:
            List of short model names (e.g. ``["fct_mrr_monthly", …]``).
        """
        self._assert_loaded()
        return [
            k[len(_MODEL_PREFIX):]
            for k in self._nodes
            if k.startswith(_MODEL_PREFIX)
        ]

    def get_source_tables(self) -> list[str]:
        """
        Return all source node names from the manifest.

        Returns:
            List of source unique_id strings.
        """
        self._assert_loaded()
        return list(self._sources.keys())

    def build_lineage_graph(self) -> dict[str, list[str]]:
        """
        Build an adjacency dict representing the direct upstream dependencies
        for each model.

        Returns:
            ``{model_name: [upstream_model_name, …]}`` — direct edges only.
        """
        self._assert_loaded()
        graph: dict[str, list[str]] = defaultdict(list)

        for key, node in self._nodes.items():
            if not key.startswith(_MODEL_PREFIX):
                continue
            short_name = key[len(_MODEL_PREFIX):]
            deps = node.get("depends_on", {}).get("nodes", [])
            for dep in deps:
                if dep.startswith(_MODEL_PREFIX):
                    graph[short_name].append(dep[len(_MODEL_PREFIX):])

        return dict(graph)

    def get_model_schema(self, model_name: str) -> str:
        """
        Return the Snowflake schema name for a model (e.g. 'marts', 'staging').
        """
        node = self.get_model_node(model_name)
        return node.get("schema", "")

    # ----------------------------------------------------------------- private

    def _assert_loaded(self) -> None:
        if not self._loaded:
            raise ManifestLoadError(
                "ManifestParser.load() has not been called. "
                "Ensure the startup event initialises the parser before serving requests."
            )
