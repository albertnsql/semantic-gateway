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

# Save downloaded HuggingFace models locally so Render preserves them
# between the build phase and the runtime phase.
os.environ["HF_HOME"] = os.path.abspath("./.hf_cache")

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
    # Dynamically size pool for Render's 512MB RAM constraint
    pool_size = int(os.getenv("SNOWFLAKE_POOL_SIZE", "5"))
    snowflake_pool = SnowflakePool(settings=settings, size=pool_size)
    snowflake_ok   = False
    try:
        snowflake_pool.initialise()
        snowflake_ok = True
        logger.info("✓ Snowflake pool ready.")
    except Exception as exc:
        logger.warning("✗ Snowflake pool init failed: %s", exc)

    sql_generator = SQLGenerator(settings=settings, pool=snowflake_pool if snowflake_ok else None)

    # ── 4.5. SQL Template Cache (skips MetricFlow subprocess on repeat metric/dim combos) ──
    # refresh_on_load=True: the disk file is a build artifact (pre-compiled via
    # precompile_templates.py and committed to the repo). Templates only go stale
    # when the dbt semantic model changes, which always ships as a new deploy that
    # replaces the file — so entries are re-stamped with a fresh TTL at startup.
    sql_template_cache = SQLTemplateCache(
        ttl_seconds=settings.sql_template_cache_ttl_seconds,
        maxsize=settings.sql_template_cache_maxsize,
        disk_path="./.sql_template_cache.json",
        refresh_on_load=True,
    )
    sql_generator._template_cache = sql_template_cache  # inject after construction
    logger.info(
        "✓ SQLTemplateCache ready (TTL: %ds, disk: ./.sql_template_cache.json). "
        "Compiled SQL templates survive restarts — MetricFlow subprocess skipped for "
        "any previously-seen metric+dimension combination.",
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
    if os.getenv("DISABLE_RAG", "false").lower() == "true":
        logger.info("✓ RAG disabled via environment variable. Falling back to full metric injection (uses less RAM).")
    elif _chroma_populated:
        try:
            from rag.embedder import MetricEmbedder
            metric_embedder = MetricEmbedder(persist_dir=_chroma_dir)
            logger.info("✓ ChromaDB index found, loading…")
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
    query_cache = QueryCache(
        ttl_seconds=_ttl,
        maxsize=settings.query_cache_maxsize,
        disk_path="./.query_cache.json",
    )
    app.state.query_cache = query_cache
    logger.info(
        "✓ Query cache initialized (TTL: %ds, maxsize: %d, Disk: ./.query_cache.json).",
        _ttl,
        settings.query_cache_maxsize,
    )

    # ── 7. (removed) Intent classifier ──────────────────────────────────────
    # Routing (metric_query / schema_question / out_of_scope) is now decided
    # inside the IntentExtractor's single LLM call — one round trip instead of
    # two on every query. classifier.py remains only for shared helpers.

    # ── 8. Cache Warmer ────────────────────────────────────────────────
    if os.getenv("DISABLE_CACHE_WARMER", "false").lower() != "true":
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
    else:
        logger.info("✓ CacheWarmer disabled via environment variable.")

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

# CORS — configurable origins (restrict to frontend URL in production).
# Set CORS_ALLOWED_ORIGINS=https://your-frontend.onrender.com in production.
# NOTE: allow_credentials must be False when allow_origins=["*"]; that combination
# is explicitly rejected by browsers per the CORS spec.
_cors_origins = [o.strip() for o in settings.cors_allowed_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API Key authentication for cost-bearing routes ──────────────────────────
# When GATEWAY_API_KEY is set, requests to /api/v1/query and /api/v1/dashboard
# must include a matching X-API-Key header (or ?api_key= query param).
# When the key is empty (development), all requests pass through.
import secrets as _secrets

# Route prefixes that require API key authentication.
_AUTH_REQUIRED_PREFIXES = ("/api/v1/query", "/api/v1/dashboard")


@app.middleware("http")
async def api_key_auth_middleware(request: Request, call_next):
    """Enforce API key on cost-bearing routes when GATEWAY_API_KEY is configured."""
    api_key = settings.gateway_api_key
    if api_key and request.url.path.startswith(_AUTH_REQUIRED_PREFIXES):
        provided = (
            request.headers.get("X-API-Key")
            or request.query_params.get("api_key")
            or ""
        )
        if not provided or not _secrets.compare_digest(provided, api_key):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Missing or invalid API key. Provide X-API-Key header.",
                },
            )
    return await call_next(request)


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
