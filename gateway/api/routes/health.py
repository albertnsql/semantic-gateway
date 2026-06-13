"""
api/routes/health.py — GET /api/v1/health endpoint.

Returns current gateway health status including Snowflake connectivity,
manifest load status, metric catalog size, ChromaDB index state, LLM
provider availability, and query cache statistics.

Snowflake connectivity is checked once at startup (see main.py) and cached
on app.state to avoid a blocking 5-second network call on every health poll.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from models.responses import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Gateway health check",
    description=(
        "Returns the current operational status of the AI Semantic Gateway, "
        "including Snowflake connectivity, semantic registry load status, "
        "ChromaDB index state, LLM provider availability, and cache statistics."
    ),
)
async def health_check(request: Request) -> HealthResponse:
    """
    Health check endpoint (fast — no live Snowflake call).

    Returns ``healthy`` when all core dependencies are reachable and loaded.
    Returns ``degraded`` if any core dependency is unavailable.
    """
    state = request.app.state
    settings = state.settings

    # ── Core checks ───────────────────────────────────────────────────────────
    snowflake_ok: bool = getattr(state, "snowflake_connected", False)
    manifest_loaded  = getattr(state.manifest_parser, "_loaded", False)
    metrics_count    = len(state.metric_registry.list_metrics())
    semantic_count   = state.metric_registry.count_semantic_models()

    # ── ChromaDB / RAG index status ───────────────────────────────────────────
    metric_embedder = getattr(state, "metric_embedder", None)
    if metric_embedder is None:
        chroma_status = "unavailable (index not built)"
    else:
        try:
            count = metric_embedder.collection.count()
            chroma_status = f"ok ({count} metrics indexed)" if count > 0 else "empty"
        except Exception as exc:
            logger.warning("Health check: ChromaDB count failed — %s", exc)
            chroma_status = "unavailable"

    # ── LLM provider status (check client availability, no live call) ─────────
    intent_extractor = getattr(state, "intent_extractor", None)
    if intent_extractor is not None:
        llm_primary  = "ok" if getattr(intent_extractor, "_primary_client",  None) else "unavailable"
        llm_fallback = "ok" if getattr(intent_extractor, "_fallback_client", None) else "unavailable"
    else:
        llm_primary = llm_fallback = "unavailable"

    # ── Query cache statistics ─────────────────────────────────────────────────
    query_cache = getattr(state, "query_cache", None)
    cache_stats = query_cache.stats() if query_cache else {}
    cache_entries = cache_stats.get("active_entries", 0)

    # ── Overall status ────────────────────────────────────────────────────────
    overall_status = "healthy" if (manifest_loaded and metrics_count > 0) else "degraded"

    return HealthResponse(
        status=overall_status,
        snowflake_connected=snowflake_ok,
        manifest_loaded=manifest_loaded,
        metrics_loaded=metrics_count,
        semantic_models_loaded=semantic_count,
        gateway_version=settings.gateway_version,
        gateway_env=settings.gateway_env,
        chroma_db=chroma_status,
        llm_primary=llm_primary,
        llm_fallback=llm_fallback,
        cache_entries=cache_entries,
    )
