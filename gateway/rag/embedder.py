"""
gateway/rag/embedder.py — Semantic embedding and retrieval for metric definitions.

Owns all ChromaDB + sentence-transformers logic.  Keeps vector-store
operations isolated so the rest of the gateway stays decoupled from
the specific embedding model or vector-store backend.

Usage::

    embedder = MetricEmbedder()
    embedder.index_metrics(registry.get_all_metrics())
    relevant = embedder.retrieve("Show me MRR by segment", top_k=5)
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class MetricEmbedder:
    """Embeds metric definitions and retrieves the most relevant ones for a query."""

    def __init__(self, persist_dir: str = "./chroma_store") -> None:
        """Initialise the persistent ChromaDB collection."""
        import chromadb

        self._model = None
        self._model_name = "all-MiniLM-L6-v2"

        logger.info("Connecting to ChromaDB at '%s'…", persist_dir)
        self._client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self._client.get_or_create_collection(
            name="metrics",
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("MetricEmbedder ready. Collection size: %d", self.collection.count())

    @property
    def model(self):
        """Lazy-load the sentence transformer model to prevent blocking startup."""
        if self._model is None:
            import os
            os.environ["TOKENIZERS_PARALLELISM"] = "false"
            from sentence_transformers import SentenceTransformer
            logger.info("Loading SentenceTransformer model '%s'…", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def _build_document(self, metric: dict) -> str:
        """Build a plain-text sentence describing a metric for embedding."""
        name = metric.get("name", "")
        description = metric.get("description") or name

        dimensions = metric.get("dimensions", [])
        if isinstance(dimensions, list):
            dims_str = ", ".join(str(d) for d in dimensions) if dimensions else "none"
        else:
            dims_str = str(dimensions)

        grains = metric.get("time_dimensions", {})
        doc = f"{name}: {description}. Dimensions: {dims_str}."
        if grains:
            grain_values = list(grains.keys()) if isinstance(grains, dict) else grains
            if grain_values:
                doc += f" Valid time grains: {', '.join(str(g) for g in grain_values)}."
        return doc

    def index_metrics(self, metrics: list[dict]) -> None:
        """Embed and upsert all metrics into the ChromaDB collection."""
        if not metrics:
            logger.warning("index_metrics called with an empty list — nothing to index.")
            return

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict] = []

        for metric in metrics:
            name = metric.get("name", "")
            if not name:
                continue
            doc = self._build_document(metric)
            ids.append(name)
            documents.append(doc)
            metadatas.append({
                "name": name,
                "dimensions": json.dumps(metric.get("dimensions", [])),
                "grains": json.dumps(metric.get("time_dimensions", {})),
            })

        if not ids:
            logger.warning("index_metrics: no valid metric names found.")
            return

        # Batch encode all documents in one call — 3-5x faster than one-at-a-time
        logger.info("Encoding %d metric documents (batched)…", len(documents))
        embeddings_matrix = self.model.encode(
            documents, batch_size=32, show_progress_bar=False
        )
        embeddings = [vec.tolist() for vec in embeddings_matrix]

        self.collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        logger.info("Indexed %d metrics into ChromaDB.", len(ids))

    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """Embed the query and return the top_k most semantically similar metric dicts."""
        if self.collection.count() == 0:
            raise RuntimeError(
                "ChromaDB metrics collection is empty. "
                "Run: python -m gateway.rag.indexer to build the index."
            )

        query_embedding = self.model.encode(query)
        results = self.collection.query(
            query_embeddings=[query_embedding.tolist()],
            n_results=min(top_k, self.collection.count()),
            include=["metadatas", "documents"],
        )

        retrieved: list[dict] = []
        for meta in (results.get("metadatas") or [[]])[0]:
            retrieved.append({
                "name": meta.get("name", ""),
                "dimensions": json.loads(meta.get("dimensions", "[]")),
                "grains": json.loads(meta.get("grains", "{}")),
            })

        logger.debug("RAG retrieve('%s') → %d results: %s", query, len(retrieved), [m["name"] for m in retrieved])
        return retrieved
