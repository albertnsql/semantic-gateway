"""
api/routes/query.py — POST /api/v1/query main endpoint.

This is the primary entry point for the AI Semantic Gateway.  It
orchestrates the full pipeline — after a two-stage intent classification:
  1. Classify → METRIC_QUERY | SCHEMA_QUESTION | OUT_OF_SCOPE
  2. For METRIC_QUERY:
       RAG retrieval → intent extraction → semantic validation →
       MetricFlow SQL → Snowflake execution → lineage resolution →
       governed response (with result caching).
  3. For SCHEMA_QUESTION / OUT_OF_SCOPE: return template responses immediately.

No business logic lives here — the route only orchestrates service calls.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import decimal

def make_json_safe(obj):
    if isinstance(obj, list):
        return [make_json_safe(i) for i in obj]
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return obj

# Routing (metric_query / schema_question / out_of_scope) is decided inside the
# IntentExtractor call itself — no separate classifier LLM round trip.
from classifier import build_out_of_scope_suggestion

from config import settings as _settings
from core.exceptions import (
    IntentExtractionError,
    SnowflakeConnectionError,
    SQLGenerationError,
)
from models.requests import QueryRequest
from models.responses import GatewayResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Query"])


# ─────────────────────────────────────────── helper: intent dict for caching

def _intent_to_dict(intent) -> dict:
    """Serialise a QueryIntent to a plain dict for use as a cache key."""
    return intent.model_dump(mode="json", exclude={"raw_llm_response", "original_query"})


def _generate_narrative(query: str, results: list[dict], intent, settings) -> str:
    """
    Call Gemini (primary) or Groq (fallback) to produce a concise conversational
    summary of the query results for the 'Summary' tab.

    Returns an empty string on any failure so the caller can fail-open.
    """
    if not results:
        return ""
    try:
        from openai import OpenAI as _OpenAI

        # Build a compact preview (max 10 rows) so we don't blow the context
        preview_rows = results[:10]
        rows_text = "\n".join(
            ", ".join(f"{k}: {v}" for k, v in row.items()) for row in preview_rows
        )
        truncated_note = f"\n(Showing first {len(preview_rows)} of {len(results)} rows.)" if len(results) > 10 else ""

        metrics   = ", ".join(intent.metrics) if intent.metrics else "unknown metrics"
        dims      = intent.dimensions or []
        dims_str  = ", ".join(dims) if dims else "no breakdown"
        time_info = ""
        if intent.time_range:
            time_info = f" for the period {intent.time_range.start_date} to {intent.time_range.end_date}"

        row_count = len(results)
        precomputed_stats = ""
        
        if len(dims) > 0 and row_count > 0:
            try:
                numeric_keys = [k for k, v in results[0].items() if isinstance(v, (int, float))]
                str_keys = [k for k, v in results[0].items() if isinstance(v, str)]
                
                if numeric_keys:
                    num_k = numeric_keys[0]
                    dim_k = str_keys[0] if str_keys else list(results[0].keys())[0]
                    
                    valid_rows = [r for r in results if isinstance(r.get(num_k), (int, float))]
                    if valid_rows:
                        sorted_rows = sorted(valid_rows, key=lambda x: x[num_k])
                        min_row = sorted_rows[0]
                        max_row = sorted_rows[-1]
                        
                        min_value = min_row[num_k]
                        min_label = min_row.get(dim_k, "Unknown")
                        max_value = max_row[num_k]
                        max_label = max_row.get(dim_k, "Unknown")
                        metric_value = sum(r[num_k] for r in valid_rows)
                        
                        precomputed_stats = (
                            f"\nPre-computed Stats:\n"
                            f"- row_count: {row_count}\n"
                            f"- metric_value (sum of rows): {metric_value}\n"
                            f"- max_value: {max_value} (max_label: {max_label})\n"
                            f"- min_value: {min_value} (min_label: {min_label})\n"
                        )
            except Exception as e:
                logger.warning("Failed to calculate precomputed stats: %s", e)

        system_prompt = (
            "You are a data analyst assistant for a streaming analytics platform. "
            "When given query results, produce a conversational summary. "
            "CRITICAL RULES: "
            "1. If this is a dimensional breakdown, name the top and bottom segments explicitly using the values provided. Do not summarize without referencing specific dimension values. "
            "2. Always use the provided metric_value, max_value, and min_value — never estimate numbers from the preview rows. "
            "3. Produce exactly 2 sentences. First sentence: the breakdown with specific names and numbers. Second sentence: the business interpretation. "
            "4. Format all revenue and monetary values with a '$' sign, commas, and 2 decimal places (e.g., $1,234.56). Format percentages with a '%' sign and up to 2 decimal places (e.g., 25.4%). "
            "5. Wrap all numbers, percentages, and monetary values in double asterisks so they can be highlighted (e.g., **$1,234.56**, **25.4%**, or **1,234**). Do NOT use any other markdown formatting (no headers, no bullet points)."
        )
        user_prompt = (
            f"The analyst asked: \"{query}\"\n\n"
            f"This query measured {metrics} broken down by {dims_str}{time_info}.\n"
            f"{precomputed_stats}\n"
            f"Preview Rows:\n{rows_text}{truncated_note}\n\n"
            "Please provide the 2-sentence conversational summary of these results."
        )

        # Prefer Gemini, fallback to Groq.
        # Time-boxed: the narrative runs AFTER results are ready and only decorates
        # them — it must never hold the response hostage (SDK default is 600 s).
        if getattr(settings, "google_api_key", ""):
            client = _OpenAI(api_key=settings.google_api_key, base_url=settings.google_base_url,
                             timeout=8.0, max_retries=0)
            model  = settings.google_model
        else:
            client = _OpenAI(api_key=settings.openai_api_key, base_url=settings.llm_base_url,
                             timeout=8.0, max_retries=0)
            model  = settings.openai_model

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=150,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("Narrative generation failed (non-fatal): %s", exc)
        return ""


def _generate_schema_response(query: str, registry, settings) -> str:
    """Call the LLM to answer a schema question based on the metric registry."""
    metrics = registry.get_all_metrics()
    metrics_context = "\n".join([f"- {m.get('name', 'Unknown')}: {m.get('description', '')}. Dimensions: {m.get('dimensions', [])}" for m in metrics])
    
    system_prompt = (
        "You are a helpful data analyst assistant. "
        "The user is asking a question about the metrics, dimensions, or schema available in the system. "
        "Answer their question accurately using the provided catalog of metrics. "
        "Keep the response conversational, friendly, and easy to read. "
        "CRITICAL RULES: "
        "1. Write in basic human language using natural paragraphs. Do NOT use markdown headers, bolding, or bulleted lists. "
        "2. Replace all underscores (_) in metric and dimension names with spaces to make them more readable for end users (e.g. 'plan type' instead of 'plan_type')."
    )
    user_prompt = f"Available Metrics Catalog:\n{metrics_context}\n\nUser Question: {query}"
    
    try:
        from openai import OpenAI as _OpenAI
        if getattr(settings, "google_api_key", ""):
            client = _OpenAI(api_key=settings.google_api_key, base_url=settings.google_base_url,
                             timeout=8.0, max_retries=0)
            model  = settings.google_model
        else:
            client = _OpenAI(api_key=settings.openai_api_key, base_url=settings.llm_base_url,
                             timeout=8.0, max_retries=0)
            model  = settings.openai_model

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=300,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("Schema response generation failed: %s", exc)
        all_metric_names = [m.name for m in registry.list_metrics()]
        example_metric = all_metric_names[0] if all_metric_names else "mrr"
        return (
            "I can help you understand what's available in this system.\n"
            f"Here are the metrics you can query: {', '.join(all_metric_names)}\n\n"
            "For each metric, you can filter and group by various dimensions.\n"
            f"Try asking something like: \"Show me {example_metric} by plan_type\""
        )




# ─────────────────────────────────────────── POST /query

@router.post(
    "/query",
    response_model=GatewayResponse,
    summary="Submit a natural language analytics query",
    description=(
        "The primary gateway endpoint.  Accepts a natural language analytics question, "
        "classifies it (metric / schema / out-of-scope), validates it against the certified "
        "semantic registry, generates MetricFlow-governed SQL, executes against Snowflake, "
        "and returns results with full lineage context. "
        "\n\n**Rejected queries** (governance violations) return HTTP 422 with a detailed "
        "explanation and suggested fixes."
    ),
    responses={
        200: {"description": "Query validated and executed successfully."},
        400: {"description": "Intent extraction failed (malformed query or LLM error)."},
        422: {"description": "Query rejected by semantic governance."},
        500: {"description": "Internal SQL generation or Snowflake execution error."},
        503: {"description": "Snowflake unavailable."},
    },
)
async def submit_query(
    body: QueryRequest,
    request: Request,
) -> JSONResponse:
    """
    Full governance-enforced query pipeline with two-stage intent classification.

    Flow:
      0. Classify question type
      1. METRIC_QUERY → full pipeline (RAG → extract → validate → SQL → Snowflake)
      2. SCHEMA_QUESTION → return available metrics immediately
      3. OUT_OF_SCOPE → return suggestion response immediately

    Args:
        body: Validated QueryRequest body.
        request: FastAPI Request (carries app.state services).
    """
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    logger.info(
        "[%s] Incoming query: %s (dry_run=%s)",
        request_id,
        body.query[:120],
        body.options.dry_run,
    )

    # Pull services from app.state
    extractor        = request.app.state.intent_extractor
    validator        = request.app.state.semantic_validator
    sql_gen          = request.app.state.sql_generator
    lineage_resolver = request.app.state.lineage_resolver
    response_builder = request.app.state.response_builder
    registry         = request.app.state.metric_registry
    metric_embedder  = getattr(request.app.state, "metric_embedder", None)
    query_cache      = getattr(request.app.state, "query_cache", None)

    # ── Stage 1: Intent extraction (includes query_type routing) ─────────────
    try:
        available_metrics    = [m.name for m in registry.list_metrics()]
        available_dims       = registry.get_all_dimension_map()
        available_time_grains = {
            m.name: registry.get_valid_time_grains_for_metric(m.name)
            for m in registry.list_metrics()
        }
        intent = extractor.extract(
            body.query, available_metrics, available_dims, available_time_grains,
            history=body.history, retriever=metric_embedder,
            dashboard_context=body.dashboard_context,
        )
    except IntentExtractionError as exc:
        logger.error("[%s] Intent extraction failed: %s", request_id, exc)
        return JSONResponse(
            status_code=400,
            content={
                "request_id": request_id,
                "error": "intent_extraction_failed",
                "message": str(exc.message),
                "detail": exc.raw_response[:300] if exc.raw_response else None,
            },
        )

    logger.info("[%s] Query type: %s", request_id, intent.query_type)

    # ── Stage 1.25: Route on query_type (extracted in the same LLM call) ──────
    if intent.query_type == "schema_question":
        message = _generate_schema_response(body.query, registry, _settings)
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.info("[%s] Schema response returned in %.1f ms.", request_id, elapsed)
        return JSONResponse(
            status_code=200,
            content={
                "status": "schema_response",
                "message": message,
                "sql": None,
                "results": None,
                "cache_hit": False,
                "request_id": request_id,
            },
        )

    if intent.query_type == "out_of_scope":
        all_metric_names = [m.name for m in registry.list_metrics()]
        suggested_query = build_out_of_scope_suggestion(body.query, all_metric_names)
        message = (
            "That's a great question, but it requires reasoning about causes and context "
            "that goes beyond what I can answer by querying data directly.\n\n"
            "What I can tell you is the data behind it — for example:\n"
            f'"{suggested_query}"\n\n'
            "Would you like me to run that instead?"
        )
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.info("[%s] Out-of-scope response returned in %.1f ms.", request_id, elapsed)
        return JSONResponse(
            status_code=200,
            content={
                "status": "out_of_scope",
                "message": message,
                "suggested_query": suggested_query,
                "sql": None,
                "results": None,
                "cache_hit": False,
                "request_id": request_id,
            },
        )

    # ── Stage 1.5: Clarification and grain validation ─────────────────────────
    # Single pass: collect available grains/dims while checking for issues.
    # Previously two separate loops over intent.metrics; now one.
    needs_clarification = intent.needs_clarification
    clarification_msg   = intent.clarification_reason or "Please refine your query."
    available_grains: set = set()
    all_dims: set = set()

    for metric_name in intent.metrics:
        grains_map   = registry.get_valid_time_grains_for_metric(metric_name)
        valid_grains: set = set()
        for grains in grains_map.values():
            valid_grains.update(grains)
        available_grains.update(valid_grains)

        if registry.is_certified_metric(metric_name):
            metric_dims = registry.get_dimensions_for_metric(metric_name)
            all_dims.update(metric_dims)
        else:
            metric_dims = []

        # Check grain compatibility (first mismatch wins)
        if not needs_clarification and intent.aggregation_level and intent.aggregation_level not in valid_grains:
            if valid_grains:
                grains_list = ", ".join(sorted(valid_grains))
                needs_clarification = True
                clarification_msg = (
                    f"The requested time range 'last 6 months' is not supported for the metric '{metric_name}'. "
                    f"Valid granularities for {metric_name} are {grains_list}. "
                    f"Try asking: 'What is the {metric_name} by plan type for the last 3 months?' "
                    f"or use one of the available time granularities: {grains_list}."
                ) if "6" in (intent.aggregation_level or "") else (
                    f"The time granularity '{intent.aggregation_level}' is not available for '{metric_name}'. "
                    f"Valid options are: {grains_list}."
                )
            else:
                needs_clarification = True
                clarification_msg = (
                    f"The time granularity '{intent.aggregation_level}' is not supported for '{metric_name}'. "
                    "Please refine your query."
                )

        # Check dimension certification
        if not needs_clarification:
            # The semantic_validator has the logic to strip metricflow prefixes
            validator = request.app.state.semantic_validator
            for dim in intent.dimensions:
                bare_dim = validator._get_bare_dimension(dim)
                if bare_dim not in metric_dims:
                    needs_clarification = True
                    clarification_msg = f"Dimension '{dim}' is not available for '{metric_name}'."
                    break

    if needs_clarification:
        logger.info("[%s] Query requires clarification: %s", request_id, clarification_msg)
        return JSONResponse(
            status_code=422,
            content={
                "status": "needs_clarification",
                "message": clarification_msg,
                "available_options": {
                    "time_grains": list(available_grains),
                    "dimensions": list(all_dims),
                },
                "sql": None,
                "results": None,
                "cache_hit": False,
            },
        )

    # ── Stage 2: Intent-keyed cache check ────────────────────────────────────
    intent_dict = _intent_to_dict(intent)
    if query_cache is not None:
        cached_result = query_cache.get(intent_dict)
        if cached_result is not None:
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.info("[%s] CACHE HIT — %.1f ms.", request_id, elapsed)
            cached_result = dict(cached_result)
            cached_result["request_id"] = request_id
            cached_result["cache_hit"] = True
            cached_result = make_json_safe(cached_result)
            resp = JSONResponse(status_code=200, content=cached_result)
            resp.headers["X-Cache"] = "HIT"
            return resp

    # ── Stage 3: Semantic validation ──────────────────────────────────────────
    validation = validator.validate(intent)

    if not validation.safe_to_execute:
        logger.warning(
            "[%s] Query REJECTED. violations=%d",
            request_id,
            len(validation.violations),
        )
        rejection_response = response_builder.build_rejection(intent, validation)
        return JSONResponse(
            status_code=422,
            content=rejection_response.model_dump(mode="json"),
        )

    logger.info("[%s] Semantic validation PASSED.", request_id)

    # ── Stage 4: Dry run — skip execution ─────────────────────────────────────
    if body.options.dry_run:
        try:
            gen_query = sql_gen.generate(intent, validation)
        except SQLGenerationError as exc:
            logger.error("[%s] SQL generation failed: %s", request_id, exc)
            return JSONResponse(
                status_code=500,
                content={
                    "request_id": request_id,
                    "error": "sql_generation_failed",
                    "message": str(exc.message),
                },
            )
        lineage = None
        if intent.metrics and body.options.include_lineage:
            try:
                lineage = lineage_resolver.resolve_metric(intent.metrics[0])
            except Exception:
                pass
        dry_run_response = response_builder.build_dry_run(intent, validation, gen_query, lineage)
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.info("[%s] Dry run completed in %.1f ms.", request_id, elapsed)
        return JSONResponse(
            status_code=200,
            content=dry_run_response.model_dump(mode="json"),
        )

    # ── Stage 5: SQL generation ────────────────────────────────────────────────
    try:
        gen_query = sql_gen.generate(intent, validation)
    except SQLGenerationError as exc:
        logger.error("[%s] SQL generation failed: %s", request_id, exc)
        return JSONResponse(
            status_code=500,
            content={
                "request_id": request_id,
                "error": "sql_generation_failed",
                "message": str(exc.message),
                "mf_command": exc.mf_command,
            },
        )

    # ── Stage 6: Snowflake execution ───────────────────────────────────────────
    results: list[dict] = []
    try:
        all_rows = sql_gen.execute_query(gen_query.compiled_sql)
        results  = all_rows[: body.options.max_rows]
        logger.info(
            "[%s] Snowflake returned %d rows (capped at %d).",
            request_id, len(all_rows), body.options.max_rows,
        )
    except SnowflakeConnectionError as exc:
        logger.error("[%s] Snowflake error: %s", request_id, exc)
        return JSONResponse(
            status_code=503,
            content={
                "request_id": request_id,
                "error": "snowflake_unavailable",
                "message": str(exc.message),
            },
        )

    # ── Stage 7: Lineage resolution ────────────────────────────────────────────
    lineage = None
    if intent.metrics and body.options.include_lineage:
        try:
            lineage = lineage_resolver.resolve_metric(intent.metrics[0])
        except Exception as exc:
            logger.warning("[%s] Lineage resolution failed (non-fatal): %s", request_id, exc)

    # ── Stage 8: Assemble response ─────────────────────────────────────────────
    if not body.options.include_sql:
        gen_query.compiled_sql = ""

    success_response = response_builder.build_success(
        intent, validation, gen_query, results, lineage
    )

    elapsed = (time.perf_counter() - start_time) * 1000
    logger.info(
        "[%s] Request completed in %.1f ms. rows=%d status=%s",
        request_id, elapsed, len(results), success_response.status,
    )

    payload = success_response.model_dump(mode="json")
    payload["cache_hit"] = False

    # ── Stage 8b: Conversational narrative summary ─────────────────────────────
    payload["narrative_summary"] = _generate_narrative(body.query, results, intent, _settings)

    # ── Store in intent-keyed cache ────────────────────────────────────────────
    payload = make_json_safe(payload)
    if query_cache is not None:
        query_cache.set(intent_dict, payload)

    resp = JSONResponse(status_code=200, content=payload)
    resp.headers["X-Cache"] = "MISS"
    return resp


# ─────────────────────────────────────────── POST /cache/clear (admin utility)

@router.post(
    "/cache/clear",
    summary="Clear the query result cache",
    description=(
        "Evicts all entries from the in-memory query result cache. "
        "Requires X-Admin-Key header matching ADMIN_SECRET_KEY in .env."
    ),
    tags=["Query"],
)
async def clear_cache(
    request: Request,
    x_admin_key: str = Header(default=""),
) -> JSONResponse:
    """
    Clear all entries from the intent-keyed query result cache.

    Requires the X-Admin-Key header to match ADMIN_SECRET_KEY from settings.
    Returns 403 if the key is missing or incorrect.
    """
    expected = _settings.admin_secret_key
    if not expected:
        # If no key is configured, lock the endpoint down entirely in production
        if _settings.gateway_env != "development":
            raise HTTPException(status_code=403, detail="Cache clear is disabled — set ADMIN_SECRET_KEY.")
    elif x_admin_key != expected:
        raise HTTPException(status_code=403, detail="Invalid admin key.")

    query_cache = getattr(request.app.state, "query_cache", None)
    if query_cache is None:
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "message": "No cache is configured."},
        )
    query_cache.clear()
    stats = query_cache.stats()
    logger.info("Cache cleared via admin endpoint.")
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "message": "Cache cleared.", "stats": stats},
    )
