"""
core/intent_extractor.py — LLM-powered NLU for analytics query intent.

Supports OpenAI (gpt-4o) and any OpenAI-compatible provider such as
Groq (llama-3.3-70b-versatile), Together AI, or Fireworks AI.
The provider is selected via the ``base_url`` constructor argument;
an empty string falls back to the default OpenAI endpoint.

Temperature is forced to 0.0 for deterministic, reproducible intent extraction.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from openai import OpenAI
from pydantic import BaseModel

from core.exceptions import IntentExtractionError

import importlib.util
import pathlib

_skill_loader_path = (
    pathlib.Path(__file__).resolve()
    .parent   # gateway/core/
    .parent   # gateway/
    .parent   # Streaming_Analytics/
    / "backend" / "core" / "skill_loader.py"
)

if _skill_loader_path.exists():
    _spec = importlib.util.spec_from_file_location("skill_loader", _skill_loader_path)
    _skill_loader_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_skill_loader_mod)  # type: ignore[union-attr]
    get_skill_section = _skill_loader_mod.get_skill_section
    _SKILL_LOADER_AVAILABLE = True
else:
    _SKILL_LOADER_AVAILABLE = False
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "backend/core/skill_loader.py not found at '%s' — skill injection disabled.",
        _skill_loader_path,
    )

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────── Data models

class TimeRange(BaseModel):
    """Resolved or relative time window extracted from the natural language query."""

    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    relative: str | None = None  # e.g. "last_30_days", "last_3_months"


class FilterClause(BaseModel):
    """A single filter predicate extracted from the natural language query."""

    column: str
    operator: str  # eq | neq | gt | gte | lt | lte | in
    value: str | list[str]


class QueryIntent(BaseModel):
    """
    Structured representation of the user's analytics query intent,
    as extracted by OpenAI from a natural language question.
    """

    original_query: str
    metrics: list[str]
    dimensions: list[str]
    filters: list[FilterClause] = []
    time_range: TimeRange | None = None
    aggregation_level: str | None = None  # monthly | weekly | daily
    order_by: str | None = None
    limit: int | None = None
    raw_llm_response: str = ""
    needs_clarification: bool = False
    clarification_reason: str | None = None


# ──────────────────────────────────────────────── Intent extractor

class IntentExtractor:
    """
    Calls OpenAI (gpt-4o, temperature=0.0) to extract a structured
    :class:`QueryIntent` from a natural language analytics question.

    The system prompt constrains the model to ONLY use metrics and
    dimensions from the certified registry — hallucination prevention
    is built into the prompt, not post-hoc filtering.

    Usage::

        extractor = IntentExtractor(api_key="sk-...", model="gpt-4o")
        intent = extractor.extract(query, available_metrics, dim_map)
    """

    def __init__(self, settings) -> None:
        """
        Args:
            settings: Gateway settings object.
        """
        # Primary Client (Google Gemini)
        self._primary_model = settings.google_model
        if settings.google_api_key:
            self._primary_client = OpenAI(
                api_key=settings.google_api_key,
                base_url=settings.google_base_url,
            )
        else:
            self._primary_client = None

        # Fallback Client (Groq)
        self._fallback_model = settings.openai_model
        self._fallback_client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.llm_base_url,
        )

        # Tertiary Client (OpenRouter)
        self._tertiary_model = settings.openrouter_model
        if settings.openrouter_api_key:
            self._tertiary_client = OpenAI(
                api_key=settings.openrouter_api_key,
                base_url=settings.openrouter_base_url,
            )
        else:
            self._tertiary_client = None

        logger.info("IntentExtractor initialised: primary=%s fallback=%s tertiary=%s", self._primary_model, self._fallback_model, self._tertiary_model)

    def extract(
        self,
        query: str,
        available_metrics: list[str],
        available_dimensions: dict[str, list[str]],
        available_time_grains: dict[str, dict[str, list[str]]],
        history: list = None,
        retriever=None,
        dashboard_context: dict | None = None,
    ) -> QueryIntent:
        """
        Call OpenAI to extract structured query intent from natural language.

        Args:
            query: The user's natural language analytics question.
            available_metrics: List of certified metric names.
            available_dimensions: Map of {metric_name: [certified_dim_names]}.
            available_time_grains: Map of {metric_name: {time_dim: [grains]}}.
            history: List of previous Message objects (role, content).

        Returns:
            A fully validated :class:`QueryIntent`.

        Raises:
            IntentExtractionError: If the LLM response cannot be parsed as JSON
                or does not conform to the QueryIntent schema.
        """
        system_prompt = self.build_system_prompt(
            available_metrics, available_dimensions, available_time_grains,
            retriever=retriever, query=query, dashboard_context=dashboard_context,
        )
        today_str = date.today().isoformat()

        messages = [
            {"role": "system", "content": system_prompt},
        ]
        if history:
            # We only keep the last 5 turns to prevent context bloat
            for msg in history[-5:]:
                # Force roles to be either 'user' or 'assistant'
                role = "assistant" if msg.role in ("agent", "assistant", "system") else "user"
                messages.append({"role": role, "content": msg.content or ""})

        messages.append(
            {
                "role": "user",
                "content": (
                    f"Today's date is {today_str}.\n\n"
                    f"Analytics question: {query}"
                ),
            }
        )

        response = None
        # Try Primary Client (OpenRouter)
        if self._primary_client:
            try:
                logger.debug("Calling primary model '%s' for intent extraction.", self._primary_model)
                response = self._primary_client.chat.completions.create(
                    model=self._primary_model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=0.0,
                    max_tokens=1024,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                logger.warning("Primary LLM (%s) failed: %s. Falling back to secondary...", self._primary_model, exc)

        # Try Fallback Client (Groq) if primary failed or wasn't configured
        if not response:
            try:
                logger.debug("Calling fallback model '%s' for intent extraction.", self._fallback_model)
                response = self._fallback_client.chat.completions.create(
                    model=self._fallback_model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=0.0,
                    max_tokens=1024,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                logger.warning("Fallback LLM (%s) failed: %s. Falling back to tertiary...", self._fallback_model, exc)

        # Try Tertiary Client (Google Gemini) if fallback failed
        if not response and self._tertiary_client:
            try:
                logger.debug("Calling tertiary model '%s' for intent extraction.", self._tertiary_model)
                response = self._tertiary_client.chat.completions.create(
                    model=self._tertiary_model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=0.0,
                    max_tokens=1024,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                raise IntentExtractionError(
                    f"All LLM API calls failed. Last error: {exc}", raw_response=str(exc)
                ) from exc
                
        if not response:
            raise IntentExtractionError("No LLM clients available to process the request.")

        raw_content = response.choices[0].message.content or ""
        logger.debug("Raw LLM response (first 500 chars): %s", raw_content[:500])

        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise IntentExtractionError(
                f"LLM returned non-JSON response: {exc}",
                raw_response=raw_content,
            ) from exc

        # Resolve relative time ranges to absolute dates
        parsed = self._resolve_time_range(parsed, today_str)

        # Normalise aggregation_level: map verbose/LLM-invented strings to
        # the MetricFlow grain names (month / week / day / year).
        # Uses a two-pass approach:
        #   1. Exact-match lookup for well-known aliases.
        #   2. Regex extraction for patterns like "13 months", "90 days", etc.
        #      where the LLM erroneously stuffed a duration into aggregation_level.
        if parsed.get("aggregation_level"):
            _AL = parsed["aggregation_level"]
            _ALIAS_MAP = {
                "monthly": "month", "weekly": "week",
                "daily": "day",    "yearly": "year",
                "period_month": "month", "period_day": "day",
                "period_week": "week",   "period_year": "year",
                "semi-annual": "month",  "semiannual": "month",
                "bimonthly": "month",    "quarterly": "quarter",
            }
            if _AL in _ALIAS_MAP:
                parsed["aggregation_level"] = _ALIAS_MAP[_AL]
            else:
                # Regex: e.g. "13 months", "last_6_months", "90_days", "2weeks"
                _unit_match = re.search(
                    r"(month|week|day|year)", _AL, re.IGNORECASE
                )
                if _unit_match:
                    # Normalise the unit word to the MetricFlow grain
                    _unit = _unit_match.group(1).lower()
                    parsed["aggregation_level"] = _unit  # already singular
                # If nothing matches, leave as-is and let grain validation handle it

        try:
            intent = QueryIntent(
                original_query=query,
                raw_llm_response=raw_content,
                **{k: v for k, v in parsed.items() if k != "original_query"},
            )
        except Exception as exc:
            raise IntentExtractionError(
                f"Could not construct QueryIntent from LLM output: {exc}",
                raw_response=raw_content,
            ) from exc

        logger.info(
            "Intent extracted: metrics=%s dims=%s time=%s",
            intent.metrics,
            intent.dimensions,
            intent.time_range,
        )
        return intent

    def build_system_prompt(
        self,
        available_metrics: list[str],
        available_dimensions: dict[str, list[str]],
        available_time_grains: dict[str, dict[str, list[str]]],
        retriever=None,
        query: str = "",
        dashboard_context: dict | None = None,
    ) -> str:
        """
        Build the OpenAI system prompt that constrains the model to the
        certified semantic registry.

        When a MetricEmbedder retriever is provided the prompt will only contain
        the top-5 most semantically relevant metrics rather than the full list,
        reducing token usage and hallucination risk.

        Kept as a separate method for unit-testability.

        Args:
            available_metrics: Full list of certified metric names (used as fallback).
            available_dimensions: Map of {metric_name: [dim_names]}.
            available_time_grains: Map of {metric_name: {time_dim: [grains]}}.
            retriever: Optional MetricEmbedder for RAG-based metric selection.
            query: The user's natural language question (needed for retrieval).

        Returns:
            The full system prompt string.
        """
        # ── RAG retrieval: pick only the most relevant metrics ────────────────
        if retriever is not None and query:
            try:
                relevant = retriever.retrieve(query, top_k=5)
                rag_metric_names = [m["name"] for m in relevant]
                # Intersect with certified list to ensure we only use valid names
                selected_metrics = [m for m in rag_metric_names if m in available_metrics]
                if not selected_metrics:
                    # Edge case: retrieval returned nothing useful — fall back
                    selected_metrics = available_metrics
                    logger.warning("RAG retrieval returned no certified metrics; falling back to full list.")
                else:
                    logger.info("RAG selected %d/%d metrics for prompt: %s",
                                len(selected_metrics), len(available_metrics), selected_metrics)
            except Exception as exc:
                logger.warning("RAG retrieval failed (%s); falling back to full metric list.", exc)
                selected_metrics = available_metrics
        else:
            selected_metrics = available_metrics

        # Build prompt sections from selected (possibly RAG-filtered) metrics
        filtered_dims = {k: v for k, v in available_dimensions.items() if k in selected_metrics}
        filtered_grains = {k: v for k, v in available_time_grains.items() if k in selected_metrics}

        metrics_section = "\n".join(
            f"  - {m}" for m in selected_metrics
        )

        from core.sql_generator import build_dimension_prefix_map
        dim_map_dynamic = build_dimension_prefix_map()

        dims_section = "\n".join(
            f"  {metric}:\n" + "\n".join(f"    - {dim_map_dynamic.get(metric, {}).get(d, d)}" for d in dims)
            for metric, dims in filtered_dims.items()
        )

        schema = """
{
  "metrics": ["<metric_name>"],
  "dimensions": ["<dimension_name>"],
  "filters": [
    {"column": "<column>", "operator": "<eq|neq|gt|gte|lt|lte|in>", "value": "<value>"}
  ],
  "time_range": {
    "start_date": "YYYY-MM-DD",
    "end_date": "YYYY-MM-DD",
    "relative": "<last_30_days|last_3_months|last_year|null>"
  },
  "aggregation_level": "<valid_granularity_or_null>",
  "order_by": "<metric_or_dimension_name|null>",
  "limit": <integer_or_null>,
  "needs_clarification": false,
  "clarification_reason": null
}
"""

        grains_instructions = []
        for metric, time_dims in filtered_grains.items():
            for d, grains in time_dims.items():
                if grains:
                    grains_instructions.append(f"For the metric '{metric}', the only valid time granularities are: {', '.join(grains)}.\nDo not invent granularities that are not in this list.\nIf the user's question implies a granularity not in this list, set a flag 'needs_clarification: true' and populate 'clarification_reason' with a plain English explanation.")

        grains_section = "\n\n".join(grains_instructions)


        # ── Skill injection: table reference + gotchas ────────────────────────
        _table_ref = ""
        _gotchas = ""
        if _SKILL_LOADER_AVAILABLE:
            try:
                _table_ref = get_skill_section("streaming_analytics", "Table Reference")
                _gotchas = get_skill_section("streaming_analytics", "Gotchas")
            except Exception as _skill_exc:
                logger.warning("Skill section load failed: %s", _skill_exc)

        _data_reference_block = ""
        if _table_ref or _gotchas:
            _data_reference_block = f"""
## Data Reference
{_table_ref}

## Query Gotchas — follow these exactly
{_gotchas}
"""

        _dashboard_context_block = ""
        if dashboard_context:
            filters = dashboard_context.get("active_filters", {})
            data_as_of = dashboard_context.get("data_as_of", "unknown")
            widgets = dashboard_context.get("visible_widgets", [])
            widget_summary = "\n".join([
                f"  - {w.get('label')}: {w.get('current_value')} (Trend: {w.get('trend', 'N/A')})"
                for w in widgets
            ])
            _dashboard_context_block = f"""
## CURRENT DASHBOARD STATE
- Active filters: {json.dumps(filters)}
- Data shown is current through: {data_as_of}
- The user is currently viewing these widgets with these values:
{widget_summary}

## RULES FOR DASHBOARD CONTEXT:
1. When the user asks about a metric visible on the dashboard without deeper breakdowns, reference the value already shown rather than re-querying. Do this by setting `metrics` to `[]`, `needs_clarification` to `true`, and writing your answer in `clarification_reason` starting with "Based on what's currently on your dashboard...".
2. When the user asks for a breakdown or deeper slice not shown on the dashboard, route that to the semantic layer as a normal new query (extract metrics/dimensions).
3. Always respect the active filter context. If filters are applied in the CURRENT DASHBOARD STATE, your extracted `filters` array MUST reflect that scope unless the user explicitly asks to ignore them.
4. Never contradict the numbers currently visible on the dashboard.
5. If the user changes a filter, the dashboard context will be re-injected — always use the most recent context provided.
"""

        return f"""You are an analytics query intent extractor for a streaming analytics platform.

Your job is to extract structured query intent from natural language analytics questions.

## CRITICAL RULES — NEVER VIOLATE:
1. You MUST respond ONLY with valid JSON. No markdown, no explanation, no code blocks.
2. You MUST ONLY use metrics from the CERTIFIED METRICS LIST below. Never invent new metric names.
3. You MUST ONLY use dimensions from the CERTIFIED DIMENSIONS MAP below. Never invent new dimension names.
4. SYNONYM MAPPING: If the user asks for a metric (e.g., "video completion rate", "revenue") or dimension (e.g., "continent", "region") that is not in the certified lists, you MUST map it to the closest semantic equivalent from the certified lists (e.g., "engagement_rate", "mrr", "country"). Do NOT ask for clarification if a reasonable mapping exists.
5. If no certified metric matches the question AT ALL (e.g., "customer satisfaction score", "performance"), use an empty list for metrics and set needs_clarification to true.
6. Only use metrics from the provided list. Do not invent metric names not in this list.

## TIME GRANULARITIES CONSTRAINTS
{grains_section}

## CERTIFIED METRICS LIST:
{metrics_section}

## CERTIFIED DIMENSIONS MAP (metric → allowed dimensions):
{dims_section}
{_data_reference_block}
{_dashboard_context_block}
## TIME RANGE RESOLUTION:
For ANY "last N <unit>" phrase (where N is any number and unit is days/weeks/months/years),
set relative to "last_N_<unit>s" format. Examples:
- "last 30 days"   → relative: "last_30_days"
- "last 3 months"  → relative: "last_3_months"
- "last 6 months"  → relative: "last_6_months"
- "last 13 months" → relative: "last_13_months"
- "last 2 weeks"   → relative: "last_2_weeks"
- "last 2 years"   → relative: "last_2_years"
- "last month"     → relative: "last_month" (first day of previous calendar month)
- "last year"      → relative: "last_year" (today minus 365 days)
- "this year"      → relative: "this_year" (Jan 1st of current year)
- "last quarter"   → relative: "last_quarter" (today minus 90 days)
- If a specific date range is mentioned, parse it directly into start_date/end_date
- If no time range is mentioned, set time_range to null

For aggregation_level, always use the base grain word: "month", "week", "day", or "year".
Never put a duration (e.g. "6 months") in aggregation_level.

## OUTPUT JSON SCHEMA:
{schema}

## EXAMPLES:

User: "What is the MRR by plan type for the last 3 months?"
Output:
{{"metrics": ["mrr"], "dimensions": ["plan_type"], "filters": [], "time_range": {{"start_date": "2024-02-27", "end_date": "2024-05-27", "relative": "last_3_months"}}, "aggregation_level": "month", "order_by": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "Show me churn rate by country this year"
Output:
{{"metrics": ["churn_rate"], "dimensions": ["country"], "filters": [], "time_range": {{"start_date": "2024-01-01", "end_date": "2024-05-27", "relative": "this_year"}}, "aggregation_level": "month", "order_by": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "What is the LTV by acquisition channel?"
Output:
{{"metrics": ["ltv"], "dimensions": ["acquisition_channel"], "filters": [], "time_range": null, "aggregation_level": null, "order_by": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}
"""

    def _resolve_time_range(self, parsed: dict, today_str: str) -> dict:
        """
        Fill in missing start_date/end_date for relative time range references.

        Handles both named ranges (last_month, this_year …) and the general
        ``last_N_<unit>s`` pattern produced for any arbitrary duration the user
        mentions (e.g. "last 13 months", "last 90 days", "last 2 weeks").

        Resolution order:
          1. Named aliases  (last_month, last_year, this_year, last_quarter)
          2. Regex pattern  last_N_(days|weeks|months|years)  — any N
          3. Fallback       last 30 days (same as before)
        """
        # Guard against malformed LLM response
        if not isinstance(parsed, dict):
            logger.warning(
                "_resolve_time_range: expected dict, got %s. Resetting.", type(parsed)
            )
            parsed = {"metrics": [], "dims": [], "time_range": None}

        tr = parsed.get("time_range")
        if not tr:
            return parsed

        today = date.fromisoformat(today_str)
        relative = (tr.get("relative") or "").strip().lower()

        if not tr.get("start_date") or not tr.get("end_date"):

            # ── Named aliases ─────────────────────────────────────────────────
            if relative == "last_month":
                first_of_month = today.replace(day=1)
                last_month_end = first_of_month - timedelta(days=1)
                tr["start_date"] = last_month_end.replace(day=1).isoformat()
                tr["end_date"]   = last_month_end.isoformat()

            elif relative in ("last_year", "last_365_days"):
                tr["start_date"] = (today - timedelta(days=365)).isoformat()
                tr["end_date"]   = today_str

            elif relative == "this_year":
                tr["start_date"] = today.replace(month=1, day=1).isoformat()
                tr["end_date"]   = today_str

            elif relative in ("last_quarter", "last_3_months"):
                tr["start_date"] = (today - timedelta(days=90)).isoformat()
                tr["end_date"]   = today_str

            else:
                # ── General pattern: last_N_days / last_N_weeks /
                #                    last_N_months / last_N_years
                # Also matches variants the LLM might emit:
                #   "last_13_months", "last_30_days", "last_2_years", etc.
                _pattern = re.match(
                    r"last[_\s](\d+)[_\s]?(day|week|month|year)s?",
                    relative,
                    re.IGNORECASE,
                )
                if _pattern:
                    n    = int(_pattern.group(1))
                    unit = _pattern.group(2).lower()
                    if unit == "day":
                        delta = timedelta(days=n)
                    elif unit == "week":
                        delta = timedelta(weeks=n)
                    elif unit == "month":
                        # Approximate: 1 month ≈ 30 days
                        delta = timedelta(days=n * 30)
                    else:  # year
                        delta = timedelta(days=n * 365)
                    tr["start_date"] = (today - delta).isoformat()
                    tr["end_date"]   = today_str
                    logger.info(
                        "_resolve_time_range: resolved '%s' → %s to %s",
                        relative, tr["start_date"], tr["end_date"],
                    )
                else:
                    # Truly unknown — default to last 30 days and log it
                    logger.warning(
                        "_resolve_time_range: unrecognised relative '%s' — "
                        "defaulting to last 30 days.",
                        relative,
                    )
                    tr["start_date"] = (today - timedelta(days=30)).isoformat()
                    tr["end_date"]   = today_str

        parsed["time_range"] = tr
        return parsed
