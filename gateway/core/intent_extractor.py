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
from pydantic import BaseModel, field_validator

from core.exceptions import IntentExtractionError

# A plain import since the skills moved inside gateway/. This was previously a
# ~20-line `importlib.util.spec_from_file_location` bootstrap that walked three
# parents up to the repo root and into `backend/core/`, because the loader lived in
# a sibling package the gateway could not import. It also meant the DEPLOYED
# gateway read its prompt grounding from outside its own root directory, and only
# worked because Render checks out the whole repo.
try:
    from core.skill_loader import get_skill_section
    _SKILL_LOADER_AVAILABLE = True
except ImportError as _exc:  # pragma: no cover - defensive
    _SKILL_LOADER_AVAILABLE = False
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "core/skill_loader.py could not be imported (%s) — skill injection disabled.",
        _exc,
    )

logger = logging.getLogger(__name__)


# Output ceiling for the intent-extraction call. Measured completions are 141-148
# tokens, so this is roughly 2x headroom. It is NOT a limit on the question -- it is
# a reservation the provider gates on before generating anything, which is how a
# 12-token question ("What is the MRR by plan type for the last 3 months?") got
# refused by OpenRouter for "requesting up to 1024 tokens".
_INTENT_MAX_TOKENS: int = 300

# Fields on MetricDefinition that must NEVER reach the prompt. `raw_yaml` is the
# metric's raw dbt source and `lineage` its raw->stg->int->mart chain; together they
# were 11,780 chars -- 27% of the whole prompt -- and neither helps a model choose
# between two metrics. The rest are registry internals for the SQL layer.
# tests/test_intent_extractor.py pins this, because the way they got in was an
# accidental f"{metric_definition}" repr rather than a decision, and the same slip
# would reintroduce all of them at once.
_PROMPT_EXCLUDED_METRIC_FIELDS: frozenset[str] = frozenset({
    "raw_yaml", "lineage", "source_model", "grain", "grain_columns",
    "allowed_joins", "fanout_risk_models", "measure_column", "time_dimension",
    "filter_expression", "certified_dimensions", "valid_time_grains",
})


# HTTP status codes and error shapes worth a second attempt on the primary rung.
# Deliberately a CLOSED list: an unrecognised error is NOT retried, because the
# failures actually seen on the dead rungs are deterministic (404 model retired,
# 402 no credit, 400 bad request) and retrying those burns the timeout budget to
# reach a byte-identical verdict. Compare `is_deterministic_mf_error()` in
# sql_generator.py, which takes the opposite default -- there an unrecognised
# error might be a genuine engine fault worth a retry; here it is almost always a
# provider saying no.
# 408 and 504 are excluded: like a client-side timeout they mean the request
# already consumed its budget without an answer.
_TRANSIENT_LLM_STATUS = frozenset({409, 429, 500, 502, 503})

_TRANSIENT_LLM_MARKERS = (
    # "timed out" / "timeout" are deliberately ABSENT — see _is_transient_llm_error.
    # A timeout is transient but expensive, and retrying one doubled a production
    # failure from 16 s to 81 s.
    "high demand",          # Google 503: "currently experiencing high demand"
    "overloaded",
    "temporarily unavailable",
    "connection error",
    "connection reset",
    "resource_exhausted",   # Google 429 quota
)


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"\A\s*```(?:json)?\s*|\s*```\s*\Z", re.IGNORECASE)


def _loads_tolerant(raw: str) -> dict:
    """
    ``json.loads`` with a salvage pass for provider-specific response wrappers.

    The happy path is unchanged -- a bare ``json.loads`` is tried FIRST, so a
    provider that honours ``response_format={"type": "json_object"}`` costs
    nothing extra and parses byte-identically to before.

    The salvage exists because the parse used to be a bare ``json.loads`` on
    ``message.content``, which made "the provider wrapped its JSON" and "the
    provider returned prose" the same unrecoverable IntentExtractionError. Two
    wrappers are common enough to be worth handling, and BOTH became live risks
    when Cerebras/qwen-3.8-27b became the primary rung on 2026-09-09:

    * ```` ```json ... ``` ```` fences, from any model that ignores JSON mode
    * ``<think>...</think>`` preambles -- the Qwen3 family is hybrid-reasoning
      and emits them unless thinking is suppressed

    Last resort is the first BALANCED ``{...}`` object, scanned with string- and
    escape-awareness so a brace inside a filter value ("Smith, John") cannot end
    the object early.

    Raises:
        json.JSONDecodeError: if nothing parses, so the caller's existing
            ``except json.JSONDecodeError`` still handles it.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    text = _JSON_FENCE_RE.sub("", _THINK_BLOCK_RE.sub("", raw).strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:index + 1])

    raise json.JSONDecodeError("no JSON object in LLM response", raw or "", 0)


def _is_timeout_error(exc: BaseException) -> bool:
    """A request that got no response at all before the client gave up."""
    status = _status_of(exc)
    if status == 408 or status == 504:
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _is_transient_llm_error(exc: BaseException) -> bool:
    """True when re-issuing the SAME request could plausibly succeed CHEAPLY.

    Transience is not the only thing that matters — the COST of being wrong does,
    and it differs by a factor of 40 between the two shapes:

    * 429 / 503 / 5xx come back in about a second, so a second attempt is nearly
      free and often succeeds. Google returns intermittent 503 "experiencing high
      demand" on this key.
    * a TIMEOUT has already spent the entire per-attempt budget. Retrying it
      doubles the user's wait for the same outcome.

    That second case is not hypothetical. It is what production did on 2026-09-08:
    `Show me total subscribers by plan type` timed out at 40 s, retried, timed out
    at 40 s again, then hit the two dead rungs — a **81,154 ms** 400. The identical
    query and prompt answer in 1.1-7.5 s locally, so Gemini was not slow, it was
    unreachable from that host, and no number of retries was going to change that.
    Before the retry existed the same failure took 16 s.

    So timeouts are deliberately NOT retried. Everything else keeps the closed-list
    treatment: an unrecognised error is not retried, because the failures actually
    seen on the dead rungs are deterministic (404 retired model, 402 no credit, 400
    bad request) and retrying those burns the budget to reach an identical verdict.
    """
    if _is_timeout_error(exc):
        return False

    status = _status_of(exc)
    if status is not None:
        return status in _TRANSIENT_LLM_STATUS

    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_LLM_MARKERS)


def _metric_name(metric: object) -> str:
    """Name of a metric given either a MetricDefinition or an already-plain string.

    Callers are inconsistent: the route passes objects, tests and the RAG branch
    pass names. Normalising here is what stops a name/object comparison from
    silently matching nothing.
    """
    if isinstance(metric, str):
        return metric
    return str(getattr(metric, "name", metric))


def _render_metric_for_prompt(metric: object, name: str) -> str:
    """One compact line per metric: what is needed to CHOOSE it, nothing else.

    `certified_dimensions` and `valid_time_grains` are deliberately excluded even
    though the model needs both -- they are rendered by the dedicated DIMENSIONS MAP
    and TIME GRANULARITIES sections, and the map is where
    build_dimension_prefix_map() converts a bare `country` into the qualified
    `subscriber__country` the compiler actually resolves. Duplicating them here
    would re-teach the bare form and undo that.
    """
    if isinstance(metric, str):
        return f"  - {metric}"
    label = getattr(metric, "label", None) or ""
    mtype = getattr(metric, "metric_type", None) or ""
    desc = getattr(metric, "description", None) or ""
    head = f"  - {name}"
    if label and label != name:
        head += f" ({label})"
    if mtype:
        head += f" [{mtype}]"
    return f"{head}: {desc}".rstrip().rstrip(":")


# Physical time columns behind the semantic layer's time dimensions. A filter on any
# of these duplicates `time_range` — see the guard in :meth:`IntentExtractor.extract`.
_TIME_DIMENSION_COLUMNS: frozenset[str] = frozenset({
    "churn_date",
    "signup_date",
    "period_month",
    "payment_date",
    "session_start",
    "event_timestamp",
    "cohort_month",
    "metric_time",
})


# ──────────────────────────────────────────────── Data models

class TimeRange(BaseModel):
    """Resolved or relative time window extracted from the natural language query."""

    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    relative: str | None = None  # e.g. "last_30_days", "last_3_months"

    @field_validator("start_date", "end_date")
    @classmethod
    def _must_be_iso_date(cls, v: str) -> str:
        """Reject dates that don't match YYYY-MM-DD to prevent injection."""
        import re as _re
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError(f"Date must be YYYY-MM-DD, got {v!r}")
        return v


class FilterClause(BaseModel):
    """A single filter predicate extracted from the natural language query."""

    column: str
    operator: str  # eq | neq | gt | gte | lt | lte | in
    value: str | list[str]

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_stringified_list(cls, v, info):
        """
        Turn a stringified list into a real list.

        Every consumer branches on ``isinstance(value, list)``, so a multi-value
        filter that arrives as one string renders as ONE SQL literal:
        ``IN ('basic,standard,premium')``. That is valid SQL which matches nothing,
        so it returns zero rows with ``status=success`` and no error anywhere, and
        the empty answer is then written to the L2 result cache. Normalising here
        fixes all three SQL paths (outer predicates, MetricFlow ``--where``, the
        fallback builder) at once rather than patching each.

        Two shapes have been observed from the model, and only the first was
        handled originally:

        * **bracketed** — ``"['basic', 'standard', 'premium']"``. Emitted when the
          model enumerates every value of a dimension it is already grouping by.
        * **bare delimited** — ``"basic,standard"``, ``"US,GB,DE"``. Emitted when
          the *user* names the values ("MRR for basic and standard plans"), which
          is the far more common case: it accounted for four of five phrasings
          when this was probed against the live model. Every one of those queries
          returned zero rows in production.

        Splitting is deliberately restricted to ``operator == "in"``, the only
        operator for which a list is meaningful. A comma inside an ``eq`` value is
        part of the value, not a separator, and must survive untouched. A single
        value stays a plain string: ``IN ('premium')`` is already correct.

        Only the comma is treated as a separator. "and" is not, because it appears
        inside legitimate dimension values.
        """
        if not isinstance(v, str):
            return v

        stripped = v.strip()

        # Bracketed forms parse regardless of operator — that was the original
        # behaviour and a bracketed string is never a scalar value.
        if stripped.startswith("[") and stripped.endswith("]"):
            import ast
            try:
                parsed = ast.literal_eval(stripped)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, (list, tuple)):
                return [str(item).strip() for item in parsed]
            # Malformed brackets fall through to the delimiter split below.

        # info.data holds the fields validated so far. `operator` is declared
        # before `value`, so it is available here; .get() covers the case where
        # operator itself failed validation.
        if (info.data or {}).get("operator") != "in":
            return v

        inner = stripped
        for opener, closer in (("[", "]"), ("(", ")")):
            if inner.startswith(opener) and inner.endswith(closer):
                inner = inner[1:-1].strip()
                break

        if "," not in inner:
            return v

        parts = [part.strip().strip("'\"").strip() for part in inner.split(",")]
        parts = [part for part in parts if part]
        return parts or v


class QueryIntent(BaseModel):
    """
    Structured representation of the user's analytics query intent,
    as extracted by OpenAI from a natural language question.
    """

    original_query: str
    metrics: list[str]
    dimensions: list[str] = []
    filters: list[FilterClause] = []
    time_range: TimeRange | None = None
    aggregation_level: str | None = None  # monthly | weekly | daily
    # `order_by` names the metric or dimension to rank by; `order_direction`
    # carries the direction, because a bare column name cannot express one.
    # BOTH are required for `limit` to be honoured -- see resolve_mf_order() in
    # sql_generator.py. A LIMIT with no ORDER BY selects an ARBITRARY row, and
    # "which country has the highest MRR" then answered with whichever row the
    # engine emitted first: Canada at $8,937.76, below the mean of a $192,078
    # total, reported to the user as the maximum.
    order_by: str | None = None
    order_direction: str | None = None  # "asc" | "desc" | None (treated as desc)
    limit: int | None = None
    raw_llm_response: str = ""
    needs_clarification: bool = False
    clarification_reason: str | None = None
    # Routing decision made in the same LLM call as extraction (replaces the
    # separate IntentClassifier round trip):
    #   metric_query | schema_question | diagnostic_query | out_of_scope
    # diagnostic_query is the only non-metric_query type that still carries
    # metrics/time_range/filters -- the diagnosis needs all three.
    query_type: str = "metric_query"


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
        # SDK-level retries stay OFF: the SDK retries every error class including
        # deterministic 4xx, which is what made a rate-limited primary block for
        # minutes before the fallback chain ran.
        #
        # The TIMEOUT is now configurable (config.llm_timeout_seconds, default 40 s)
        # and no longer 15 s. 15 s existed to reach the fallback chain quickly, and
        # that chain is currently one working provider -- Groq retired
        # `llama-3.1-8b-instant` and OpenRouter is out of credit -- so a tight
        # ceiling turns a slow success into a 400 with nothing to fall back to.
        # Measured median for the real prompt is 4.55 s; production hit 15 s twice.
        _LLM_TIMEOUT_S = float(getattr(settings, "llm_timeout_seconds", 40.0))
        _LLM_MAX_RETRIES = 0
        self._primary_retries = int(getattr(settings, "llm_primary_retries", 1))

        # Allowed filter VALUES per dimension, injected by main.py's lifespan once
        # the warehouse is open. Empty until then, which reproduces the old prompt
        # exactly -- so a warehouse that never opens degrades rather than breaks.
        self._dimension_values: dict[str, list[str]] = {}

        # Rungs come from `settings.provider_chain()`, so the ORDER is config, not
        # code. The slot names are kept — tests and the retry logic below address
        # `_primary_*` — but which provider lands in each slot is now
        # `llm_provider_order`. That matters because the right order depends on the
        # HOST: Render cannot reach generativelanguage.googleapis.com at all, while
        # the same instance reaches openrouter.ai in ~400 ms, and OpenRouter serves
        # the identical google/gemini-3.1-flash-lite.
        # `settings` is a real Settings object in production and a stand-in in
        # tests. Calling the ONE implementation unbound covers both without a
        # second copy of the ordering logic here.
        chain_fn = getattr(settings, "provider_chain", None)
        if callable(chain_fn):
            chain = chain_fn()
        else:
            from config import Settings as _Settings
            chain = _Settings.provider_chain(settings)
        # The provider LABEL has to survive into the call sites: qwen needs
        # reasoning_effort="none" and Gemini must not necessarily get it, so the
        # quirk is looked up per rung from settings.llm_request_kwargs().
        _kwargs_for = getattr(settings, "llm_request_kwargs", None)
        if not callable(_kwargs_for):
            from config import Settings as _SettingsForKwargs
            _kwargs_for = lambda lbl: _SettingsForKwargs.llm_request_kwargs(  # noqa: E731
                settings, lbl
            )

        clients: list[tuple[str, object, str]] = []
        for label, api_key, base_url, model in chain:
            clients.append((
                label,
                OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    timeout=_LLM_TIMEOUT_S,
                    max_retries=_LLM_MAX_RETRIES,
                ),
                model,
            ))

        def _slot(index: int):
            return clients[index] if index < len(clients) else (None, None, "")

        # IntentExtractor has exactly three rungs, but _chat_with_fallback()
        # iterates the WHOLE chain -- so a fourth provider would apply to the
        # narrative and schema answers and silently not to intent extraction,
        # which is the same split-brain provider_chain() was created to remove.
        if len(clients) > 3:
            logger.warning(
                "llm_provider_order lists %d providers but IntentExtractor uses "
                "only the first 3 -- %s will never serve intent extraction "
                "(though _chat_with_fallback still uses them).",
                len(clients), [label for label, _, _ in clients[3:]],
            )

        (_pl, self._primary_client, self._primary_model) = _slot(0)
        (_fl, self._fallback_client, self._fallback_model) = _slot(1)
        (_tl, self._tertiary_client, self._tertiary_model) = _slot(2)

        self._primary_kwargs = _kwargs_for(_pl) if _pl else {}
        self._fallback_kwargs = _kwargs_for(_fl) if _fl else {}
        self._tertiary_kwargs = _kwargs_for(_tl) if _tl else {}

        logger.info(
            "IntentExtractor initialised (order=%s): primary=%s fallback=%s tertiary=%s",
            getattr(settings, "llm_provider_order", "(default)"),
            self._primary_model or "(none)",
            self._fallback_model or "(none)", self._tertiary_model or "(none)",
        )

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
            # Drop blank turns BEFORE truncating. Message.content is a required str
            # but '' is valid, and a client that stores its answer somewhere other
            # than `content` will happily send empty assistant turns — which tell
            # the model it replied with nothing, and burn slots that real turns need.
            # Filtering here keeps the gateway correct regardless of the caller.
            _non_empty = [m for m in history if (m.content or "").strip()]
            _dropped = len(history) - len(_non_empty)
            if _dropped:
                logger.warning(
                    "Dropped %d/%d blank history turn(s) — the caller is not populating "
                    "'content'. The model cannot see its own prior answers.",
                    _dropped, len(history),
                )
            # We only keep the last 5 turns to prevent context bloat
            for msg in _non_empty[-5:]:
                # Force roles to be either 'user' or 'assistant'
                role = "assistant" if msg.role in ("agent", "assistant", "system") else "user"
                messages.append({"role": role, "content": msg.content.strip()})

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
            # Gemini is the intended path and the only rung that currently works, so
            # a TRANSIENT failure gets one more shot here rather than falling through
            # to two dead providers. Deterministic errors (bad model, malformed
            # request, auth) are not retried -- they would fail identically.
            for _attempt in range(1 + max(self._primary_retries, 0)):
                if response:
                    break
                try:
                    logger.debug("Calling primary model '%s' for intent extraction.", self._primary_model)
                    response = self._primary_client.chat.completions.create(
                        model=self._primary_model,
                        messages=messages,  # type: ignore[arg-type]
                        temperature=0.0,
                        # The intent JSON measures 128-174 tokens against the real
                        # prompt; 300 is ~1.7x headroom. 1024 was never a size, it
                        # was a RESERVATION, and providers gate on it: OpenRouter
                        # refused a live request for "up to 1024 tokens" when it
                        # could afford 434, for an answer needing ~145.
                        max_tokens=_INTENT_MAX_TOKENS,
                        response_format={"type": "json_object"},
                        **self._primary_kwargs,
                    )
                except Exception as exc:
                    retryable = _is_transient_llm_error(exc)
                    more = _attempt < max(self._primary_retries, 0)
                    if retryable and more:
                        logger.warning(
                            "Primary LLM (%s) failed transiently: %s. Retrying (%d/%d)...",
                            self._primary_model, exc, _attempt + 1,
                            max(self._primary_retries, 0),
                        )
                        continue
                    logger.warning(
                        "Primary LLM (%s) failed: %s. Falling back to secondary...",
                        self._primary_model, exc,
                    )
                    break

        # Try Fallback Client (Groq) if primary failed or wasn't configured
        if not response:
            try:
                logger.debug("Calling fallback model '%s' for intent extraction.", self._fallback_model)
                response = self._fallback_client.chat.completions.create(
                    model=self._fallback_model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=0.0,
                    # The intent JSON measures 141-148 tokens in practice; 300 is
                    # ~2x headroom. 1024 was never a size, it was a RESERVATION, and
                    # providers gate on it: OpenRouter refused a live request with
                    # "requested up to 1024 tokens, but can only afford 434" while
                    # the answer it would have produced needed ~145.
                    max_tokens=_INTENT_MAX_TOKENS,
                    response_format={"type": "json_object"},
                    **self._fallback_kwargs,
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
                    # The intent JSON measures 141-148 tokens in practice; 300 is
                    # ~2x headroom. 1024 was never a size, it was a RESERVATION, and
                    # providers gate on it: OpenRouter refused a live request with
                    # "requested up to 1024 tokens, but can only afford 434" while
                    # the answer it would have produced needed ~145.
                    max_tokens=_INTENT_MAX_TOKENS,
                    response_format={"type": "json_object"},
                    **self._tertiary_kwargs,
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
            parsed = _loads_tolerant(raw_content)
        except json.JSONDecodeError as exc:
            raise IntentExtractionError(
                f"LLM returned non-JSON response: {exc}",
                raw_response=raw_content,
            ) from exc

        # Normalise query_type — routing decision extracted in the same call.
        # Anything unrecognised falls back to metric_query (fail-open: the
        # semantic validator still guards the pipeline downstream).
        _qt = str(parsed.get("query_type") or "metric_query").strip().lower()
        if _qt not in ("metric_query", "schema_question", "diagnostic_query", "out_of_scope"):
            _qt = "metric_query"
        parsed["query_type"] = _qt

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

        # Drop filters that merely restate the time range. With conversation history
        # enabled the LLM started emitting predicates like
        # `subscriber__churn_date__day >= 2025-01-01` alongside an identical
        # time_range; pushed through MetricFlow's --where that double-constrains the
        # query. Time belongs in time_range only. A deterministic guard here means we
        # do not depend on the prompt rule holding.
        if parsed.get("time_range") and parsed.get("filters"):
            _kept, _dropped = [], []
            for _f in parsed["filters"]:
                _col = str((_f or {}).get("column", "")).lower()
                _bare = _col.split("__")[-1] if "__" in _col else _col
                # strip a trailing grain suffix, e.g. churn_date__day → churn_date
                if _col.endswith(("__day", "__week", "__month", "__quarter", "__year")):
                    _bare = _col.rsplit("__", 1)[0].split("__")[-1]
                (_dropped if _bare in _TIME_DIMENSION_COLUMNS else _kept).append(_f)
            if _dropped:
                logger.info(
                    "Dropped %d time-dimension filter(s) that duplicate time_range: %s",
                    len(_dropped), [d.get("column") for d in _dropped],
                )
                parsed["filters"] = _kept

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

        # Filters are logged with their COERCED python type, not just their text.
        # A multi-value filter that stays a str renders as one SQL literal and
        # silently returns zero rows, and the previous log line printed metrics,
        # dimensions and time but not filters — so the one field that caused the
        # bug was the one field invisible in production.
        logger.info(
            "Intent extracted: metrics=%s dims=%s time=%s filters=%s",
            intent.metrics,
            intent.dimensions,
            intent.time_range,
            [
                f"{f.column} {f.operator} {f.value!r}({type(f.value).__name__})"
                for f in intent.filters
            ],
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

        # `available_metrics` arrives as MetricDefinition OBJECTS (the route passes
        # registry.list_user_facing_metrics()) while `available_dimensions` and
        # `available_time_grains` are keyed by metric NAME. Comparing the two
        # directly matched 0 of 23 keys, so filtered_dims and filtered_grains were
        # both silently EMPTY and the "CERTIFIED DIMENSIONS MAP" and "TIME
        # GRANULARITIES CONSTRAINTS" sections rendered blank. Nothing failed: the
        # model still saw dimension names, but only as a side effect of the
        # full-object repr below -- which taught it BARE names and never the
        # prefixed ones dims_section builds via build_dimension_prefix_map(). That
        # is the likely origin of invented prefixes such as `session__country`.
        # Normalise to names so both shapes work and neither section can silently
        # empty again.
        selected_names = [_metric_name(m) for m in selected_metrics]
        metric_objects = {_metric_name(m): m for m in selected_metrics}

        filtered_dims = {k: v for k, v in available_dimensions.items() if k in selected_names}
        filtered_grains = {k: v for k, v in available_time_grains.items() if k in selected_names}

        # Render only what CHOOSING a metric requires. This used to be f"  - {m}"
        # on the MetricDefinition itself, i.e. a pydantic repr, so every request
        # shipped each metric's `raw_yaml` (6,010 chars) and `lineage` (5,770) --
        # the raw dbt source and the raw->stg->int->mart chain. Neither helps pick
        # between `mrr` and `net_mrr_growth`, and lineage is Stage 7 response
        # metadata that leaked into the Stage 1 prompt. Nobody chose to send them;
        # the object was simply interpolated into a string.
        metrics_section = "\n".join(
            _render_metric_for_prompt(metric_objects[n], n) for n in selected_names
        )

        from core.dimension_values import format_for_prompt
        # Without this the model emits whatever the user said -- "Germany" against a
        # warehouse storing "DE" -- and the filter matches zero rows with
        # status=success. See core/dimension_values.py.
        _filter_values_block = format_for_prompt(getattr(self, "_dimension_values", {}))

        from core.sql_generator import build_dimension_prefix_map
        dim_map_dynamic = build_dimension_prefix_map()

        # QUALIFIED names via dim_map_dynamic (`country` -> `subscriber__country`),
        # exactly as the original code did. I changed this to bare names on the
        # theory that qualified names caused `multi-metric-001` to hedge with
        # needs_clarification, then measured it properly: the real cause was my
        # rewording of grains_section, and bare names cost BOTH `clarification`
        # cases (0/2 bare vs 2/2 qualified, three runs each -- consistent, not
        # noise). So the prefix rewrite CLAUDE.md describes as "the LLM's
        # vocabulary" is correct and stays.
        #
        # Note this section was live for the eval harness all along (it passes
        # metric NAMES) and dead for the route (which passes MetricDefinition
        # OBJECTS -- see the keying fix above). So production has been running
        # without it, and fixing the keying is what finally applies it there.
        dims_section = "\n".join(
            f"  {metric}:\n" + "\n".join(f"    - {dim_map_dynamic.get(metric, {}).get(d, d)}" for d in dims)
            for metric, dims in filtered_dims.items()
        )

        schema = """
{
  "query_type": "<metric_query|schema_question|diagnostic_query|out_of_scope>",
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
  "order_direction": "<asc|desc|null>",
  "limit": <integer_or_null>,
  "needs_clarification": false,
  "clarification_reason": null
}
"""

        # Wording left EXACTLY as it was, deliberately. I rewrote this into a
        # compact form (state the rule once, then one line per metric) and it cost
        # `multi-metric-001` -- the model began setting needs_clarification=true on
        # "MRR and churn rate by country for last quarter" even though `quarter` is
        # a valid grain for both metrics. Reverting the wording fixed it.
        #
        # Worth knowing WHY that was measurable at all: callers disagree about this
        # argument's type. The eval harness passes metric NAMES (run_evals.py:176,
        # matching this method's `list[str]` annotation) so it always reached this
        # loop, while the route passes MetricDefinition OBJECTS and so never did.
        # The route path was therefore running with no granularity constraints at
        # all, and the evals were the only thing exercising this text. Compacting
        # it looked free and was not.
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
1. When the user asks about a metric visible on the dashboard without deeper breakdowns (AND their question does not ask for a different time period or filter than the active ones), reference the value already shown rather than re-querying. Do this by setting `metrics` to `[]`, `needs_clarification` to `true`, and writing your answer in `clarification_reason` starting with "Based on what's currently on your dashboard...".
2. When the user asks for a breakdown, deeper slice, or a different time period/filter not currently shown on the dashboard, route that to the semantic layer as a normal new query (extract metrics/dimensions).
3. Always respect the active filter context. If filters are applied in the CURRENT DASHBOARD STATE, your extracted `filters` array MUST reflect that scope unless the user explicitly asks to ignore them or change them.
4. Never contradict the numbers currently visible on the dashboard.
5. If the user changes a filter, the dashboard context will be re-injected — always use the most recent context provided.
"""

        return f"""You are an analytics query router and intent extractor for a streaming analytics platform.

Your job is to FIRST classify the question, THEN (for metric queries) extract structured query intent.

## STEP 1 — CLASSIFY (set "query_type"):
- "metric_query": The question asks for specific data, numbers, or metrics that can be
  answered by querying a data warehouse. Examples: "Show me MRR by segment",
  "What was churn last month?", "Compare revenue across regions".
- "schema_question": The question asks about what data or metrics exist, what dimensions
  are available, or how the system works. Examples: "What metrics do you have?",
  "What dimensions can I filter by?", "What does MRR mean in this system?"
- "diagnostic_query": The question asks WHY a certified metric moved, or what is
  DRIVING / CAUSING / EXPLAINING a change in it. Examples: "Why did MRR drop?",
  "Why is revenue lower in Germany?", "What's driving the churn increase?",
  "Explain the fall in engagement".
- "out_of_scope": The question cannot be answered from this warehouse at all —
  predictions, recommendations, or subjects with no certified metric. Examples:
  "What should I focus on?", "Predict next quarter's revenue",
  "Why are competitors growing faster?"

The line between the last two is whether a CERTIFIED METRIC is named or clearly
implied. "Why did MRR drop" is diagnostic because mrr exists; "why are competitors
growing" is out of scope because nothing in the warehouse measures competitors.

Also keep the boundary with "metric_query" sharp — it turns on whether an
EXPLANATION is being requested, not on the presence of a comparison:
- "revenue by country"                     -> metric_query
- "revenue by country vs last quarter"     -> metric_query  (asks for the numbers)
- "why is revenue down in Germany"         -> diagnostic_query
- "what caused the drop in revenue"        -> diagnostic_query

For "diagnostic_query" you MUST still extract `metrics` (exactly one), and
`time_range` and `filters` if the question gives them — the diagnosis needs the
metric to decompose, the period to compare, and the scope to hold constant. Leave
`dimensions` empty: which dimensions to decompose by is decided downstream from a
curated map, not by you.

For "schema_question" and "out_of_scope", set metrics to [] and stop — do not extract
dimensions, filters, or time ranges. Dashboard-context answers (see rules below, if
present) are always "metric_query".

## STEP 2 — EXTRACT (only for "metric_query"). CRITICAL RULES — NEVER VIOLATE:
1. You MUST respond ONLY with valid JSON. No markdown, no explanation, no code blocks.
2. You MUST ONLY use metrics from the CERTIFIED METRICS LIST below. Never invent new metric names.
3. You MUST ONLY use dimensions from the CERTIFIED DIMENSIONS MAP below. Never invent new dimension names.
4. SYNONYM MAPPING: If the user asks for a metric (e.g., "video completion rate", "revenue") or dimension (e.g., "continent", "region") that is not in the certified lists, you MUST map it to the closest semantic equivalent from the certified lists (e.g., "engagement_rate", "mrr", "country"). Do NOT ask for clarification if a reasonable mapping exists.
5. If the question IS a data question but no certified metric matches it AT ALL (e.g., "customer satisfaction score"), keep query_type "metric_query", use an empty list for metrics, and set needs_clarification to true.
6. Only use metrics from the provided list. Do not invent metric names not in this list.
7. When more than one certified metric matches the user's wording, resolve it with the
   METRIC DISAMBIGUATION rules below. Never pick arbitrarily between near-synonyms —
   the same question must always resolve to the same metric.

## CONVERSATION HISTORY — scope rules, NEVER VIOLATE

Earlier turns may appear before the current question. They exist ONLY to resolve a
question that is grammatically incomplete on its own.

1. `filters` MUST come from the CURRENT question. Never carry a filter forward from
   an earlier turn. If the user narrowed to one country last turn and this turn asks
   a broader question, the broader question has NO filter.
2. `metrics`, `dimensions` and `time_range` likewise come from the current question
   whenever it states them.
3. Use history ONLY when the current question cannot stand alone — a bare pronoun or
   fragment such as "and for 2025?", "what about premium?", "break that down by
   country". Then inherit the missing parts and nothing else.
4. If the current question is self-contained, IGNORE history completely. "How many
   subscribers churned in 2025" is self-contained: it means ALL subscribers, even if
   the previous turn asked about the US.
5. Never emit a filter on a metric's own time dimension (churn_date, period_month,
   signup_date, payment_date, session_start, event_timestamp). Time belongs in
   `time_range`; duplicating it as a filter double-constrains the query.

## METRIC DISAMBIGUATION — apply before choosing a metric

RATE BEATS COUNT. When the certified list contains both a rate/percentage metric and a
raw count metric for the same concept, and the user did NOT explicitly ask for a count,
you MUST choose the RATE. Counts are confounded by segment size — the largest segment
almost always has the biggest count, which makes a count-based breakdown misleading when
the user is comparing segments. Only choose the count when the user's wording explicitly
asks for one: "how many", "number of", "count of", "total ...s".

Applying that rule to this registry:
- "churn", "churn rate", "churned", "attrition", "cancellations", "% churning"
  → `churn_rate`   (the default for ANY bare mention of churn)
- "how many churned", "number of churned subscribers", "churn count", "churned users count"
  → `churned_subscribers`
- "retention", "retention rate", "% retained", "how sticky"
  → `retention_rate`
- "revenue", "sales", "income" → `total_revenue`;  "MRR", "recurring revenue" → `mrr`

CHURN IS MONTHLY AND EVENT-BASED. `churn_rate` is computed on fct_mrr_monthly and
time-filtered by the month the churn EVENT happened. This is the governed definition and
it matches the dashboard. `churned_subscribers` is a dim_subscribers snapshot count on a
different grain, so the two are NOT interchangeable and their numbers will not reconcile —
which is exactly why the rule above is mandatory rather than advisory.

## RANKING -- superlatives and top-N

A superlative or top-N question needs THREE fields set TOGETHER. `limit` is
IGNORED unless "order_by" and "order_direction" are both set, because a limit
without a sort returns an arbitrary row rather than the best one:

- "order_by": the metric or dimension to rank by (normally the metric asked about)
- "order_direction": "desc" for highest / top / most / best / largest,
                     "asc"  for lowest / bottom / least / worst / smallest
- "limit": how many rows to return ("which country" -> 1, "top 5 plans" -> 5)

The dimension being ranked STILL belongs in "dimensions". "Which country has the
highest MRR" is grouped by country and limited to 1. It is NOT a filter, and you
must never invent a specific country value for it.

A plain breakdown ("MRR by country", "churn rate by plan type") is NOT a ranking:
leave all three null so every row is returned.

## TIME GRANULARITIES CONSTRAINTS
{grains_section}

## CERTIFIED METRICS LIST:
{metrics_section}

## CERTIFIED DIMENSIONS MAP (metric → allowed dimensions):
{dims_section}
{_filter_values_block}
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

Return exactly ONE JSON object, never a JSON array and never several objects.
A question naming SEVERAL metrics is still ONE object: put every metric in the
`metrics` list and share the same dimensions, filters and time_range.

CORRECT   for "MRR and churn rate by country for last quarter":
  {{"metrics": ["mrr", "churn_rate"], "dimensions": ["country"], ...}}
WRONG - never split one question into one object per metric:
  [{{"metrics": ["mrr"], ...}}, {{"metrics": ["churn_rate"], ...}}]

## EXAMPLES:

User: "What is the MRR by plan type for the last 3 months?"
Output:
{{"query_type": "metric_query", "metrics": ["mrr"], "dimensions": ["plan_type"], "filters": [], "time_range": {{"start_date": "2024-02-27", "end_date": "2024-05-27", "relative": "last_3_months"}}, "aggregation_level": "month", "order_by": null, "order_direction": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "Show me churn rate by country this year"
Output:
{{"query_type": "metric_query", "metrics": ["churn_rate"], "dimensions": ["country"], "filters": [], "time_range": {{"start_date": "2024-01-01", "end_date": "2024-05-27", "relative": "this_year"}}, "aggregation_level": "month", "order_by": null, "order_direction": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "Show churn by plan type for 2025"
(A bare "churn" with no count wording → churn_rate, never churned_subscribers.)
Output:
{{"query_type": "metric_query", "metrics": ["churn_rate"], "dimensions": ["plan_type"], "filters": [], "time_range": {{"start_date": "2025-01-01", "end_date": "2025-12-31", "relative": null}}, "aggregation_level": "month", "order_by": null, "order_direction": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "How many subscribers churned in 2025, by plan type?"
(Explicit count wording → churned_subscribers.)
Output:
{{"query_type": "metric_query", "metrics": ["churned_subscribers"], "dimensions": ["plan_type"], "filters": [], "time_range": {{"start_date": "2025-01-01", "end_date": "2025-12-31", "relative": null}}, "aggregation_level": null, "order_by": null, "order_direction": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "What is the LTV by acquisition channel?"
Output:
{{"query_type": "metric_query", "metrics": ["ltv"], "dimensions": ["acquisition_channel"], "filters": [], "time_range": null, "aggregation_level": null, "order_by": null, "order_direction": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "Which country has the highest MRR for the month of August 2026?"
(Superlative -> group by the dimension, rank by the metric, limit 1. NOT a filter.)
Output:
{{"query_type": "metric_query", "metrics": ["mrr"], "dimensions": ["country"], "filters": [], "time_range": {{"start_date": "2026-08-01", "end_date": "2026-08-31", "relative": null}}, "aggregation_level": "month", "order_by": "mrr", "order_direction": "desc", "limit": 1, "needs_clarification": false, "clarification_reason": null}}

User: "Show me the 5 plan types with the lowest retention rate"
(Explicitly "lowest" -> order_direction is "asc", never the desc default.)
Output:
{{"query_type": "metric_query", "metrics": ["retention_rate"], "dimensions": ["plan_type"], "filters": [], "time_range": null, "aggregation_level": null, "order_by": "retention_rate", "order_direction": "asc", "limit": 5, "needs_clarification": false, "clarification_reason": null}}

User: "What metrics can I ask about?"
Output:
{{"query_type": "schema_question", "metrics": [], "dimensions": [], "filters": [], "time_range": null, "aggregation_level": null, "order_by": null, "order_direction": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "Why did churn increase last quarter?"
Output:
{{"query_type": "diagnostic_query", "metrics": ["churn_rate"], "dimensions": [], "filters": [], "time_range": {{"start_date": "2024-01-01", "end_date": "2024-03-31", "relative": null}}, "aggregation_level": null, "order_by": null, "order_direction": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "Why is revenue lower in Germany?"
Output:
{{"query_type": "diagnostic_query", "metrics": ["total_revenue"], "dimensions": [], "filters": [{{"column": "country", "operator": "eq", "value": "DE"}}], "time_range": null, "aggregation_level": null, "order_by": null, "order_direction": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}

User: "Why are our competitors growing faster than us?"
Output:
{{"query_type": "out_of_scope", "metrics": [], "dimensions": [], "filters": [], "time_range": null, "aggregation_level": null, "order_by": null, "order_direction": null, "limit": null, "needs_clarification": false, "clarification_reason": null}}
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
