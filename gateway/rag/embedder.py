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
        import os
        from chromadb import Documents, EmbeddingFunction, Embeddings
        from openai import OpenAI

        # ---------------------------------------------------------
        # Lightweight API-based Embedding Function
        # Replaces sentence-transformers/PyTorch to save >150MB RAM!
        # ---------------------------------------------------------
        class GeminiEmbeddingFunction(EmbeddingFunction):
            def __init__(self):
                self.api_key = os.getenv("GOOGLE_API_KEY")
                
            def __call__(self, input: Documents) -> Embeddings:
                if not self.api_key:
                    logger.warning("GOOGLE_API_KEY not set! Embeddings will fail.")
                    return [[0.0] * 768 for _ in input]
                    
                import requests
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents?key={self.api_key}"
                
                reqs = []
                for text in input:
                    reqs.append({
                        "model": "models/gemini-embedding-2",
                        "content": {"parts": [{"text": text}]}
                    })
                    
                # Timeout is mandatory: without it a stalled embedding endpoint
                # hangs the entire request pipeline indefinitely.
                resp = requests.post(url, json={"requests": reqs}, timeout=(3, 10))
                if not resp.ok:
                    raise RuntimeError(f"Gemini API error: {resp.text}")
                    
                data = resp.json()
                return [obj["values"] for obj in data.get("embeddings", [])]

        self._embedding_function = GeminiEmbeddingFunction()

        logger.info("Connecting to ChromaDB at '%s'…", persist_dir)
        self._client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self._client.get_or_create_collection(
            name="metrics",
            metadata={"hnsw:space": "cosine"},
            embedding_function=self._embedding_function,
        )
        logger.info("MetricEmbedder ready. Collection size: %d", self.collection.count())

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

        # Batch encode happens automatically in ChromaDB upsert if documents are passed
        # and embedding_function is attached to the collection.
        logger.info("Upserting %d metrics into ChromaDB (will call Embedding API)…", len(documents))

        self.collection.upsert(
            ids=ids,
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

        results = self.collection.query(
            query_texts=[query],
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
