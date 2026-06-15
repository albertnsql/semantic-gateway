"""
main.py — AI Semantic Gateway FastAPI application entry point.

Wires all services together, registers routers, and configures middleware.
Services are initialised once at startup and stored on app.state for
dependency-free access in route handlers.

# Run with:
#     uvicorn main:app --reload --port 8000

"""

from __future__ import annotations

import logging
import os

# Skip HuggingFace network checks for locally cached models to fix cold-start latency
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

import time
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from config import settings
from cache import QueryCache
from classifier import IntentClassifier
from core.intent_extractor import IntentExtractor
from core.lineage_resolver import LineageResolver
from core.manifest_parser import ManifestParser
from core.metric_registry import MetricRegistry
from core.response_builder import ResponseBuilder
from core.semantic_validator import SemanticValidator
from core.snowflake_pool import SnowflakePool
from core.sql_generator import SQLGenerator
from core.sql_template_cache import SQLTemplateCache
from api.routes import health, lineage, metrics, query, dashboard

# ──────────────────────────────────────────────── Logging configuration

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────── Lifespan (startup / shutdown)

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan: initialise all core services on startup.

    Services are stored on ``app.state`` so route handlers can access them
    via ``request.app.state.<service>``.
    """
    logger.info("=" * 60)
    logger.info("AI Semantic Gateway starting up…")
    logger.info("Environment : %s", settings.gateway_env)
    logger.info("OpenAI model: %s", settings.openai_model)
    logger.info("=" * 60)

    # ── 1. Parse the dbt manifest ─────────────────────────────────────────────
    manifest_parser = ManifestParser()
    try:
        manifest_parser.load(settings.manifest_path)
        logger.info("✓ Manifest loaded from '%s'.", settings.manifest_path)
    except Exception as exc:
        logger.error("✗ Failed to load manifest: %s", exc)
        logger.warning("Gateway will start in DEGRADED mode — manifest unavailable.")

    # ── 2. Load metric + semantic registry ────────────────────────────────────
    metric_registry = MetricRegistry()
    try:
        metric_registry.load(
            settings.metrics_path,
            settings.semantic_models_path,
            manifest_parser,
        )
        metric_count = len(metric_registry.list_metrics())
        logger.info("✓ MetricRegistry loaded: %d metrics.", metric_count)
    except Exception as exc:
        logger.error("✗ Failed to load metrics: %s", exc)
        metric_count = 0
        logger.warning("Gateway will start in DEGRADED mode — metrics unavailable.")

    # ── 3. Initialise core services ───────────────────────────────────────────
    intent_extractor = IntentExtractor(settings=settings)

    semantic_validator = SemanticValidator(registry=metric_registry)
    lineage_resolver = LineageResolver(
        manifest_parser=manifest_parser,
        metric_registry=metric_registry,
    )
    response_builder = ResponseBuilder(metric_registry=metric_registry)

    # ── 4. Open Snowflake connection pool (replaces per-query connect) ───────
    # Pool size 15: 11 parallel dashboard widgets + headroom for concurrent chat/query requests
    snowflake_pool = SnowflakePool(settings=settings, size=15)
    snowflake_ok   = False
    try:
        snowflake_pool.initialise()
        snowflake_ok = True
        logger.info("✓ Snowflake pool ready.")
    except Exception as exc:
        logger.warning("✗ Snowflake pool init failed: %s", exc)

    sql_generator = SQLGenerator(settings=settings, pool=snowflake_pool if snowflake_ok else None)

    # ── 4.5. SQL Template Cache (skips MetricFlow subprocess on repeat metric/dim combos) ──
    sql_template_cache = SQLTemplateCache(
        ttl_seconds=settings.sql_template_cache_ttl_seconds,
        maxsize=200,
    )
    sql_generator._template_cache = sql_template_cache  # inject after construction
    logger.info(
        "✓ SQLTemplateCache ready (TTL: %ds). MetricFlow subprocess will be skipped "
        "for any metric+dimension combination seen before.",
        settings.sql_template_cache_ttl_seconds,
    )
    
    # Pre-build dynamic dimension prefix map
    try:
        from core.sql_generator import build_dimension_prefix_map
        build_dimension_prefix_map()
    except Exception as exc:
        logger.warning("✗ Failed to pre-build dimension prefix map: %s", exc)

    # ── 4.5. RAG / Metric Embedder ────────────────────────────────────────
    metric_embedder = None
    _chroma_dir = "./chroma_store"
    _chroma_populated = (
        os.path.isdir(_chroma_dir)
        and any(True for _ in os.scandir(_chroma_dir))
    )
    if _chroma_populated:
        try:
            from rag.embedder import MetricEmbedder
            metric_embedder = MetricEmbedder(persist_dir=_chroma_dir)
            logger.info("✓ ChromaDB index found, loading…")
            # Eagerly warm the SentenceTransformer model so the first user query
            # does not pay the ~60s cold-start penalty for lazy model loading.
            try:
                _ = metric_embedder.model  # triggers SentenceTransformer download/load
                logger.info("✓ SentenceTransformer model warmed (ready for first query).")
            except Exception as warm_exc:
                logger.warning("✗ SentenceTransformer pre-warm failed (%s) — will load on first query.", warm_exc)
        except Exception as exc:
            logger.warning("✗ Could not load MetricEmbedder (%s) — falling back to full metric injection.", exc)
    else:
        logger.warning(
            "ChromaDB index not found. Run: python -m gateway.rag.indexer  "
            "(RAG disabled — using full metric injection as fallback)"
        )

    # ── 5. Attach to app.state ────────────────────────────────────────────────
    app.state.settings = settings
    app.state.manifest_parser = manifest_parser
    app.state.metric_registry = metric_registry
    app.state.intent_extractor = intent_extractor
    app.state.semantic_validator = semantic_validator
    app.state.sql_generator = sql_generator
    app.state.sql_template_cache = sql_template_cache
    app.state.lineage_resolver = lineage_resolver
    app.state.response_builder = response_builder
    app.state.snowflake_pool = snowflake_pool
    app.state.snowflake_connected = snowflake_ok
    app.state.metric_embedder = metric_embedder

    # ── 6. Query result cache ────────────────────────────────────────────────
    _ttl = settings.cache_ttl_seconds
    query_cache = QueryCache(ttl_seconds=_ttl, disk_path="./.query_cache.json")
    app.state.query_cache = query_cache
    logger.info("✓ Query cache initialized (TTL: %ds, Disk: ./.query_cache.json).", _ttl)

    # ── 7. Intent classifier (two-stage routing) ───────────────────────────
    try:
        # Reuse the primary client already created by IntentExtractor
        _classifier_client = intent_extractor._primary_client or intent_extractor._fallback_client
        _classifier_model  = (
            settings.google_model if intent_extractor._primary_client
            else settings.openai_model
        )
        intent_classifier = IntentClassifier(
            llm_client=_classifier_client,
            model=_classifier_model,
        )
        app.state.intent_classifier = intent_classifier
        logger.info("✓ IntentClassifier ready (model: %s).", _classifier_model)
    except Exception as exc:
        logger.warning("✗ IntentClassifier init failed (%s) — all queries will be treated as METRIC_QUERY.", exc)
        app.state.intent_classifier = None

    # ── 8. Cache Warmer ────────────────────────────────────────────────
    try:
        from core.cache_warmer import CacheWarmer
        warmer = CacheWarmer(
            settings=settings,
            sql_generator=sql_generator,
            query_cache=query_cache,
            response_builder=response_builder
        )
        app.state.cache_warmer = warmer
        warmer.start()
    except Exception as exc:
        logger.error("✗ Failed to start CacheWarmer: %s", exc)

    logger.info("=" * 60)
    logger.info(
        "Gateway ready. %d metrics loaded. Listening for requests…",
        metric_count,
    )
    logger.info("=" * 60)

    yield  # application runs here

    logger.info("AI Semantic Gateway shutting down.")
    try:
        if hasattr(app.state, "cache_warmer"):
            app.state.cache_warmer.stop()
        snowflake_pool.close_all()
    except Exception:
        pass


# ──────────────────────────────────────────────── FastAPI application

app = FastAPI(
    title="AI Semantic Gateway",
    description=(
        "A production-grade semantic governance layer between OpenAI LLMs and "
        "a dbt-modeled Snowflake warehouse. Enforces MetricFlow semantic contracts "
        "on every analytics query — preventing hallucinated joins, grain mismatches, "
        "and raw table bypasses."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# ──────────────────────────────────────────────── Middleware

# CORS — wildcard in development (no credentials needed), restrict in production.
# NOTE: allow_credentials must be False when allow_origins=["*"]; that combination
# is explicitly rejected by browsers per the CORS spec.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """
    Log every request with method, path, duration, and status code.
    Injects a unique X-Request-ID header into both the request state
    and the response.
    """
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        logger.error(
            "[%s] %s %s → ERROR in %.1f ms: %s",
            request_id,
            request.method,
            request.url.path,
            elapsed,
            exc,
        )
        raise

    elapsed = (time.perf_counter() - start) * 1000
    logger.info(
        "[%s] %s %s → %d in %.1f ms",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
    )

    response.headers["X-Request-ID"] = request_id
    return response


# ──────────────────────────────────────────────── Global exception handler

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all handler for any unhandled exception.

    Returns a structured JSON error response and logs the full traceback.
    Prevents raw Python tracebacks from leaking to API consumers.
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    logger.error(
        "[%s] Unhandled exception on %s %s:\n%s",
        request_id,
        request.method,
        request.url.path,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={
            "request_id": request_id,
            "error": "internal_server_error",
            "message": "An unexpected error occurred. Please check the gateway logs.",
            "path": request.url.path,
        },
    )


# ──────────────────────────────────────────────── Router registration

app.include_router(query.router,     prefix="/api/v1")
app.include_router(metrics.router,   prefix="/api/v1")
app.include_router(lineage.router,   prefix="/api/v1")
app.include_router(health.router,    prefix="/api/v1")
app.include_router(dashboard.router, prefix="/api/v1")


# ──────────────────────────────────────────────── Root redirect

@app.get("/", include_in_schema=False)
async def root():
    """Redirect root to the interactive API docs."""
    return RedirectResponse(url="/docs")

# Trigger reload for new metrics 3
