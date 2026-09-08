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

import hashlib
import logging
import time
import uuid
from datetime import date
from functools import partial

import anyio
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

_ARTIFACTS = None

router = APIRouter(tags=["Query"])


# ─────────────────────────────────────────── helper: intent dict for caching

def _intent_to_dict(intent) -> dict:
    """Serialise a QueryIntent to a plain dict for use as a cache key."""
    return intent.model_dump(mode="json", exclude={"raw_llm_response", "original_query"})


def _chat_with_fallback(
    settings,
    system_prompt: str,
    user_prompt: str,
    *,
    purpose: str,
    max_tokens: int,
    temperature: float = 0.3,
    total_budget_s: float = 12.0,
    per_attempt_s: float = 6.0,
) -> str:
    """
    Ask the LLM, walking the same three providers the IntentExtractor uses.

    The two callers below each built ONE client from
    ``if google_api_key: gemini else: groq``, so the else branch only fired when
    the key was ABSENT, never when the call FAILED. The comment claimed "Prefer
    Gemini, fallback to Groq" but nothing implemented it, and with
    ``max_retries=0`` a slow Gemini was simply a hard failure.

    Both failed in production on 2026-08-21 within one session: a schema question
    returned nothing after 10.7s, and a metric answer lost its prose while still
    returning correct rows, which the UI showed as "No conversational summary
    available for this result".

    Bounded by a total DEADLINE rather than a per-attempt timeout, so walking
    three providers cannot turn a decoration into a slow response. Returns an
    empty string when every provider fails; both callers already handle that.
    """
    from openai import OpenAI as _OpenAI

    # Same ordered chain the IntentExtractor uses. This function used to build its
    # own hardcoded google -> groq -> openrouter list, so reordering would have
    # applied to intent extraction and silently NOT to the narrative and schema
    # answers — the two paths would have disagreed about which provider is primary.
    providers: list[tuple[str, str, str, str]] = settings.provider_chain()

    if not providers:
        logger.warning("%s: no LLM provider is configured.", purpose)
        return ""

    deadline = time.perf_counter() + total_budget_s
    last_error: Exception | None = None

    for index, (label, api_key, base_url, model) in enumerate(providers):
        remaining = deadline - time.perf_counter()
        if remaining <= 0.5:
            logger.warning(
                "%s: %.1fs budget exhausted before trying %s.", purpose, total_budget_s, label
            )
            break
        try:
            client = _OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=min(per_attempt_s, remaining),
                max_retries=0,
            )
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                if index > 0:
                    logger.info(
                        "%s: served by %s (%s) after %d earlier provider(s) failed.",
                        purpose, label, model, index,
                    )
                return text
            logger.warning("%s: %s (%s) returned empty content.", purpose, label, model)
        except Exception as exc:
            last_error = exc
            logger.warning("%s: %s (%s) failed: %s", purpose, label, model, exc)

    logger.warning(
        "%s: every provider failed (%d tried, last error: %s).",
        purpose, len(providers), last_error,
    )
    return ""


def _generate_narrative(query: str, results: list[dict], intent, settings) -> str:
    """
    Call Gemini (primary) or Groq (fallback) to produce a concise conversational
    summary of the query results for the 'Summary' tab.

    Returns an empty string on any failure so the caller can fail-open.
    """
    if not results:
        # Deterministic message rather than silence. An empty result used to render as
        # "No conversational summary available for this result", which reads like the
        # summariser broke rather than like the query legitimately matched nothing —
        # and gave no hint that a filter might be the reason.
        _scope = ""
        _filters = getattr(intent, "filters", None) or []
        if _filters:
            _scope = " with " + ", ".join(
                f"{f.column} {f.operator} {f.value}" for f in _filters
            )
        _window = ""
        if getattr(intent, "time_range", None):
            _window = f" between {intent.time_range.start_date} and {intent.time_range.end_date}"
        return (
            f"No rows matched this query. **{', '.join(getattr(intent, 'metrics', []) or ['the metric'])}**"
            f"{_window}{_scope} returned no data. If you expected results, check whether the "
            "time range covers loaded data or whether a filter is narrower than intended."
        )
    try:
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
        
        # A ranked/limited result is a SLICE, not a distribution. Computing
        # min/max/sum over it and labelling them extremes and a total is how
        # "which country has the highest MRR" (limit 1) produced "highest
        # Canada, lowest Canada, the entire recorded revenue is concentrated
        # within a single geographic market" -- three claims, none checkable
        # from one row, and the third refuted by the $192,078 total the same
        # conversation had already returned.
        _limit = getattr(intent, "limit", None)
        _order_by = getattr(intent, "order_by", None)
        _direction = (getattr(intent, "order_direction", None) or "desc").strip().lower()

        # row_count <= _limit is what proves the ordering was actually APPLIED.
        # The governed fallback builder ignores both order_by and limit (it sorts
        # by the group-by column), so on that degraded path intent.limit is set
        # while every row comes back -- and framing 15 rows as "the 1 with the
        # highest mrr" would be a fresh false claim rather than a fixed one.
        if _limit and 0 < row_count <= _limit:
            _rank_word = "lowest" if _direction.startswith("asc") else "highest"
            precomputed_stats = (
                f"\nRanked Subset - the {row_count} row(s) with the {_rank_word} "
                f"{_order_by or 'value'}. This is NOT the full breakdown:\n"
                f"- rows_returned: {row_count}\n"
                f"- ranked_by: {_order_by or 'unknown'} ({_rank_word} first)\n"
                f"- total_segments: UNKNOWN - the query returned only the top "
                f"{row_count}, so these rows cannot be summed and their spread "
                f"says nothing about the segments not shown.\n"
            )
        elif len(dims) > 0 and row_count > 0:
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
            "1. If a 'Pre-computed Stats' block is present, name the top and bottom segments explicitly using the values provided. Do not summarize without referencing specific dimension values. "
            "2. Always use the numbers given in the stats block — never estimate numbers from the preview rows. "
            "3. Produce exactly 2 sentences. First sentence: the breakdown with specific names and numbers. Second sentence: the business interpretation. "
            "4. Format all revenue and monetary values with a '$' sign, commas, and 2 decimal places (e.g., $1,234.56). Format percentages with a '%' sign and up to 2 decimal places (e.g., 25.4%). "
            "5. Wrap all numbers, percentages, and monetary values in double asterisks so they can be highlighted (e.g., **$1,234.56**, **25.4%**, or **1,234**). Do NOT use any other markdown formatting (no headers, no bullet points). "
            # You see values, not semantics. You cannot tell whether the metric chosen
            # upstream answers the question that was asked, so stating a cause or
            # prescribing an action lends unearned confidence to an unverified choice.
            # A previous version wrote 'retention efforts should be prioritized in the
            # US market' from three numbers, while the query was silently unfiltered.
            "6. DESCRIBE, do not advise. State what the numbers show and, at most, what "
            "they imply about the segments named. NEVER assert a cause ('because', "
            "'driven by', 'due to') and NEVER recommend an action ('should', 'needs to', "
            "'consider', 'prioritise', 're-evaluate'). You are given values only — you "
            "cannot see why they are what they are. "
            "7. Describe ONLY the scope you were given. If a filter is listed, say so "
            "explicitly ('among premium subscribers…'); if none is listed, do not imply "
            "one. "
            # Rule 1 mandates naming a top AND a bottom, which is exactly wrong
            # for a top-N slice: with limit=1 the min and max ARE the same row,
            # and a model told to name both duly wrote "the highest was Canada,
            # while the lowest performing country was Canada".
            "8. If a 'Ranked Subset' block is present INSTEAD of 'Pre-computed "
            "Stats', the rows are the top N by the stated ranking and NOT the "
            "whole population. Name them with their values and state the ranking "
            "('the highest MRR'). Do NOT name a lowest or bottom segment, do NOT "
            "add the rows up or call any figure a total, and do NOT claim the "
            "population is concentrated, uniform, or limited to what is shown - "
            "you do not know how many segments exist. For rule 3's second "
            "sentence, interpret only the segment(s) named."
        )
        user_prompt = (
            f"The analyst asked: \"{query}\"\n\n"
            f"This query measured {metrics} broken down by {dims_str}{time_info}.\n"
            f"{precomputed_stats}\n"
            f"Preview Rows:\n{rows_text}{truncated_note}\n\n"
            "Please provide the 2-sentence conversational summary of these results."
        )

        # The narrative runs AFTER results are ready and only decorates them, so it
        # gets the tighter of the two budgets: losing the prose is survivable,
        # holding the whole response is not.
        return _chat_with_fallback(
            settings, system_prompt, user_prompt,
            purpose="Narrative generation", max_tokens=150, total_budget_s=10.0,
        )
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
        # A schema answer IS the whole response rather than a decoration, so it gets
        # a longer budget. Falling through to the deterministic catalogue below is
        # still far better than returning nothing at all.
        answer = _chat_with_fallback(
            settings, system_prompt, user_prompt,
            purpose="Schema response", max_tokens=300, total_budget_s=18.0,
        )
        if answer:
            return answer
        raise RuntimeError("no provider produced a schema answer")
    except Exception as exc:
        logger.warning("Schema response generation failed: %s", exc)
        all_metric_names = [m.name for m in registry.list_user_facing_metrics()]
        example_metric = all_metric_names[0] if all_metric_names else "mrr"
        return (
            "I can help you understand what's available in this system.\n"
            f"Here are the metrics you can query: {', '.join(all_metric_names)}\n\n"
            "For each metric, you can filter and group by various dimensions.\n"
            f"Try asking something like: \"Show me {example_metric} by plan_type\""
        )




# ─────────────────────────────────────────── POST /query

def _artifact_registry():
    """Memoised known-artifact registry. Parsing a small YAML per request is waste."""
    global _ARTIFACTS
    if _ARTIFACTS is None:
        from core.diagnostics.artifacts import ArtifactRegistry
        _ARTIFACTS = ArtifactRegistry.load()
    return _ARTIFACTS


def _run_diagnosis(body, intent, request: Request, request_id: str) -> dict | None:
    """
    Run the diagnostic graph for one "why" question.

    Returns None on ANY problem — langgraph unavailable, no playbook for the metric,
    an exception mid-run — so the caller falls through to the out-of-scope reply,
    which is where these questions went before this path existed. A diagnosis is an
    upgrade over that answer, never a way to fail a request that used to succeed.

    Runs in a worker thread (the graph and every service under it are sync), so this
    must not touch the event loop.
    """
    try:
        from core.diagnostics.graph import (
            get_diagnostic_agent,
            initial_state,
            service_config,
        )
        from core.diagnostics.state import Budget
        from core.diagnostics.windows import Window, trailing_months
    except Exception as exc:  # langgraph absent, or a broken install
        logger.warning("[%s] diagnostics import failed: %s", request_id, exc)
        return None

    agent = get_diagnostic_agent()
    if agent is None:
        return None

    metric = intent.metrics[0]

    # The question's own period is the target when it gave one. Otherwise the last N
    # WHOLE months: `inclusive=False` drops the current month because
    # fct_mrr_monthly's spine runs to current_date() while cancellations carry a
    # +1 month offset, so the newest month is structurally churn-only and reads as a
    # collapse. See core/diagnostics/windows.py.
    if intent.time_range:
        target = Window.of(intent.time_range.start_date, intent.time_range.end_date)
    else:
        target = trailing_months(
            date.today(), _settings.diagnostics_default_months, inclusive=False
        )

    budget = Budget(
        max_probes=_settings.diagnostics_max_probes,
        deadline_seconds=_settings.diagnostics_deadline_seconds,
    )
    state = initial_state(
        body.query, metric, target,
        request_id=request_id,
        filters=list(intent.filters or []),
        max_dimensions=_settings.diagnostics_max_dimensions,
        budget=budget,
    )
    config = service_config(
        request.app.state.semantic_validator,
        request.app.state.sql_generator,
        query_cache=getattr(request.app.state, "query_cache", None),
        registry=getattr(request.app.state, "metric_registry", None),
        thread_id=request_id,
    )

    try:
        final = agent.invoke(state, config)
    except Exception as exc:
        logger.warning("[%s] diagnosis failed: %s", request_id, exc, exc_info=True)
        return None

    answer = (final.get("answer") or "").strip()
    if not answer:
        return None

    plan = final.get("plan")
    return {
        "answer": answer,
        "metric": metric,
        "target_window": str(target),
        "comparison_window": str(plan.comparison) if plan else None,
        "dimensions_examined": list(plan.dimensions) if plan else [],
        "hypotheses": [
            {
                "dimension": h.dimension,
                "statement": h.statement,
                "verdict": h.verdict,
                "confidence": h.confidence,
                "explained_share": round(h.explained_share, 4),
                "evidence": list(h.evidence),
            }
            for h in (final.get("hypotheses") or [])
        ],
        # The evidence table is what makes a causal claim checkable rather than
        # plausible, so it ships with the answer rather than staying in the logs.
        "evidence": [
            {
                "id": f.id,
                "label": f.label,
                "metric": f.metric,
                "dimensions": list(f.dimensions),
                "row_count": f.row_count,
                "from_cache": f.from_cache,
                "error": f.error or None,
                "sql": f.sql if body.options.include_sql else "",
            }
            for f in (final.get("findings") or [])
        ],
        # Surfaced separately from the prose so the UI can lead with them: a
        # warning that the finding may be a data artefact has to be read BEFORE the
        # finding, not after it.
        "data_warnings": [
            {"id": a.id, "severity": a.severity, "summary": a.summary,
             "guidance": a.guidance}
            for a in (
                _artifact_registry().applicable(metric, target, plan.comparison)
                if plan else []
            )
        ],
        "cautions": list(plan.cautions) if plan else [],
        "notes": list(plan.notes) if plan else [],
        "stopped_because": final.get("stopped_because") or "",
    }


def _raw_query_cache_key(body) -> dict:
    """
    Cache key for the RAW question, used before intent extraction runs.

    The intent-keyed L2 cache sits AFTER the first LLM call, so a repeated
    question still pays it in full. Production showed `CACHE HIT - 5462.1 ms`:
    the lookup is instant, the 5.4 seconds is Gemini re-deriving an intent it had
    already derived. Extraction and the narrative are roughly 90% of a round trip.

    Everything that can change the extracted intent goes in the key, because two
    requests sharing it must be guaranteed to produce the same answer:

    * the question, whitespace-normalised and case-folded;
    * the conversation history, because a fragment ("and for 2025?") inherits from
      it, so the same text with different history is a different question;
    * whether dashboard_context was supplied, since only the dashboard chat sends
      it and its prompt block changes the answer;
    * max_rows, which changes the payload that would be replayed.

    Deliberately NOT a substitute for the L2 cache: two phrasings of one question
    share an intent but not a raw key, and L2 still catches those.
    """
    history = body.history or []
    fingerprint = "|".join(
        f"{getattr(m, 'role', '')}:{(getattr(m, 'content', '') or '').strip()}"
        for m in history
    )
    return {
        "type": "raw_query",
        "q": " ".join((body.query or "").split()).lower(),
        "h": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16],
        "dash": bool(body.dashboard_context),
        "max_rows": getattr(body.options, "max_rows", None),
    }


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

    # ── Stage 0: Raw-question cache ──────────────────────────────────────────
    # In front of extraction, so an exact repeat skips BOTH LLM calls instead of
    # only the warehouse round trip. See _raw_query_cache_key for what is in the key.
    _raw_key = _raw_query_cache_key(body)
    if query_cache is not None:
        _raw_hit = query_cache.get(_raw_key)
        if _raw_hit is not None:
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.info(
                "[%s] RAW CACHE HIT — %.1f ms (skipped intent extraction).",
                request_id, elapsed,
            )
            _raw_hit = dict(_raw_hit)
            _raw_hit["request_id"] = request_id
            _raw_hit["cache_hit"] = True
            _raw_hit = make_json_safe(_raw_hit)
            resp = JSONResponse(status_code=200, content=_raw_hit)
            resp.headers["X-Cache"] = "HIT-RAW"
            return resp

    # ── Stage 1: Intent extraction (includes query_type routing) ─────────────
    try:
        # User-facing list only: ratio building blocks (monthly_churned_subscribers,
        # monthly_subscriber_base) must not reach the prompt as selectable metrics.
        _user_facing         = registry.list_user_facing_metrics()
        available_metrics    = [m.name for m in _user_facing]
        available_dims       = registry.get_all_dimension_map()
        available_time_grains = {
            m.name: registry.get_valid_time_grains_for_metric(m.name)
            for m in _user_facing
        }
        intent = await anyio.to_thread.run_sync(
            partial(
                extractor.extract,
                body.query, available_metrics, available_dims, available_time_grains,
                history=body.history, retriever=metric_embedder,
                dashboard_context=body.dashboard_context,
            )
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

    # A filter value outside the dimension's actual values matches zero rows and
    # returns status=success -- the failure `core/dimension_values.py` exists to
    # prevent. The prompt now lists the allowed values, so this should be rare;
    # logged rather than rejected until we can see how often the model still slips,
    # because rejecting on an incomplete value list would break working queries.
    if intent.filters:
        try:
            from core.dimension_values import unknown_filter_values

            _unknown = unknown_filter_values(
                intent.filters, getattr(request.app.state, "dimension_values", {})
            )
            if _unknown:
                logger.warning(
                    "[%s] filter value(s) not present in the warehouse: %s "
                    "- this query will match zero rows",
                    request_id,
                    "; ".join(f"{col}={val!r}" for col, val in _unknown),
                )
        except Exception:
            pass

    # ── Stage 1.25: Route on query_type (extracted in the same LLM call) ──────
    if intent.query_type == "schema_question":
        message = await anyio.to_thread.run_sync(
            partial(_generate_schema_response, body.query, registry, _settings)
        )
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

    # ── Stage 1.3: Diagnostic ("why") path ────────────────────────────────────
    # Everything below is a fall-through: an unavailable graph, an unknown metric or
    # a failed run all end up in the out_of_scope branch, which is exactly where
    # these questions went before this path existed. So the worst case is the old
    # behaviour, never a 500.
    if intent.query_type == "diagnostic_query":
        diagnosis = None
        if not _settings.diagnostics_enabled:
            logger.info("[%s] diagnostics disabled - treating as out of scope", request_id)
        elif not intent.metrics:
            logger.info("[%s] diagnostic query named no metric", request_id)
        else:
            diagnosis = await anyio.to_thread.run_sync(
                partial(_run_diagnosis, body, intent, request, request_id)
            )

        if diagnosis is not None:
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.info(
                "[%s] Diagnosis returned in %.1f ms (%d probe(s)).",
                request_id, elapsed, len(diagnosis.get("evidence") or []),
            )
            return JSONResponse(
                status_code=200,
                content=make_json_safe({
                    "status": "diagnosis",
                    "message": diagnosis["answer"],
                    "narrative_summary": diagnosis["answer"],
                    "diagnosis": diagnosis,
                    "sql": None,
                    "results": None,
                    "cache_hit": False,
                    "request_id": request_id,
                }),
            )
        # fall through to out_of_scope

    if intent.query_type in ("out_of_scope", "diagnostic_query"):
        all_metric_names = [m.name for m in registry.list_user_facing_metrics()]
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
            gen_query = await anyio.to_thread.run_sync(partial(sql_gen.generate, intent, validation))
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
        gen_query = await anyio.to_thread.run_sync(partial(sql_gen.generate, intent, validation))
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
        all_rows = await anyio.to_thread.run_sync(partial(sql_gen.execute_query, gen_query.compiled_sql))
        results  = all_rows[: body.options.max_rows]
        logger.info(
            # Names the CONFIGURED engine, not Snowflake. A Snowflake-flavoured
            # message once sent a whole investigation at a dead dependency for a day
            # (see CLAUDE.md on execute_query never falling back to Snowflake).
            "[%s] %s returned %d rows (capped at %d).",
            request_id, _settings.warehouse_engine.capitalize(),
            len(all_rows), body.options.max_rows,
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
    payload["narrative_summary"] = await anyio.to_thread.run_sync(
        partial(_generate_narrative, body.query, results, intent, _settings)
    )

    # ── Store in intent-keyed cache ────────────────────────────────────────────
    payload = make_json_safe(payload)
    if query_cache is not None:
        await anyio.to_thread.run_sync(partial(query_cache.set, intent_dict, payload))
        # Also under the raw question, so the next identical ask skips both LLM calls.
        await anyio.to_thread.run_sync(partial(query_cache.set, _raw_key, payload))

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
