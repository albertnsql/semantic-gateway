"""
tests/test_intent_extractor.py — Unit tests for IntentExtractor.

The OpenAI client is mocked so these tests are fast, free, and offline.

FIXES vs original:
  - IntentExtractor.__init__ takes `settings`, not (api_key, model) — all
    constructors now pass a MagicMock settings object.
  - extract() takes an extra `available_time_grains` positional arg — added.
  - build_system_prompt() also takes `available_time_grains` — added.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import IntentExtractionError
from core.intent_extractor import FilterClause, IntentExtractor, QueryIntent, TimeRange


# ──────────────────────────────────────────────── Shared constants

AVAILABLE_METRICS = ["mrr", "ltv", "engagement_rate", "churn_rate", "expansion_mrr"]

AVAILABLE_DIMENSIONS: dict[str, list[str]] = {
    "mrr": ["plan_type", "billing_cycle", "mrr_type", "period_month"],
    "ltv": ["payment_method", "currency", "acquisition_channel", "payment_date"],
    "engagement_rate": ["device_type", "quality_streamed", "referral_source", "session_start"],
    "churn_rate": ["country", "plan_type", "acquisition_channel", "age_group"],
    "expansion_mrr": ["plan_type", "billing_cycle", "mrr_type"],
}

AVAILABLE_TIME_GRAINS: dict[str, dict[str, list[str]]] = {
    "mrr": {"period_month": ["day", "week", "month", "quarter", "year"]},
    "ltv": {"payment_date": ["day", "week", "month"]},
    "churn_rate": {"period_month": ["month", "quarter"]},
    "engagement_rate": {"session_start": ["day", "week"]},
    "expansion_mrr": {"period_month": ["month", "quarter"]},
}


# ──────────────────────────────────────────────── Helpers

def _make_settings(**overrides) -> MagicMock:
    """Build a minimal mock settings object matching IntentExtractor's expectations."""
    settings = MagicMock()
    settings.google_api_key = "fake-google-key"
    settings.google_model = "gemini-2.5-flash"
    settings.google_base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    settings.openai_api_key = "fake-groq-key"
    settings.openai_model = "llama-3.3-70b-versatile"
    settings.llm_base_url = "https://api.groq.com/openai/v1"
    settings.openrouter_api_key = ""   # no tertiary by default
    settings.openrouter_model = ""
    settings.openrouter_base_url = ""
    for k, v in overrides.items():
        setattr(settings, k, v)
    return settings


def _make_extractor(**settings_overrides) -> IntentExtractor:
    """Return an IntentExtractor backed by mock LLM clients."""
    settings = _make_settings(**settings_overrides)
    with patch("core.intent_extractor.OpenAI"):
        extractor = IntentExtractor(settings)
    return extractor


def _mock_llm_response(content: str) -> MagicMock:
    """Build a mock LLM ChatCompletion response."""
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


def _valid_intent_json(
    metrics=None, dimensions=None, time_range=None, filters=None
) -> str:
    import json
    return json.dumps({
        "metrics": metrics or ["mrr"],
        "dimensions": dimensions or [],
        "filters": filters or [],
        "time_range": time_range,
        "aggregation_level": "monthly",
        "order_by": None,
        "limit": None,
        "needs_clarification": False,
        "clarification_reason": None,
    })


# ──────────────────────────────────────────────── Basic extraction


class TestExtractSimple:
    def test_extract_mrr_metric(self) -> None:
        """'what is MRR last month' should extract metrics=['mrr']."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(
            metrics=["mrr"],
            time_range={"start_date": "2024-04-01", "end_date": "2024-04-30", "relative": "last_month"},
        )
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract(
            "what is MRR last month",
            AVAILABLE_METRICS,
            AVAILABLE_DIMENSIONS,
            AVAILABLE_TIME_GRAINS,
        )
        assert "mrr" in intent.metrics
        assert intent.original_query == "what is MRR last month"

    def test_extract_preserves_original_query(self) -> None:
        """The original query string must be preserved verbatim in the returned intent."""
        extractor = _make_extractor()
        query = "Show me MRR and churn rate by country"
        mock_json = _valid_intent_json(metrics=["mrr", "churn_rate"], dimensions=["country"])
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract(query, AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert intent.original_query == query

    def test_extract_empty_query_raises(self) -> None:
        """An empty string should still call the LLM and parse its response (no short-circuit crash)."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(metrics=[], dimensions=[])
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        # Should not raise — even empty queries pass through to LLM
        intent = extractor.extract("", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert isinstance(intent, QueryIntent)


class TestExtractWithDimension:
    def test_extract_mrr_by_plan_type(self) -> None:
        """'MRR by plan type' should extract dimensions=['plan_type']."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(metrics=["mrr"], dimensions=["plan_type"])
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract("MRR by plan type", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert "plan_type" in intent.dimensions

    def test_extract_multiple_dimensions(self) -> None:
        """Multiple dimensions should all appear in the result."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(metrics=["mrr"], dimensions=["plan_type", "billing_cycle"])
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract(
            "MRR by plan type and billing cycle", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS
        )
        assert "plan_type" in intent.dimensions
        assert "billing_cycle" in intent.dimensions


class TestExtractTimeRange:
    def test_extract_last_30_days(self) -> None:
        """'last 30 days' should produce a non-None time_range."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(
            metrics=["mrr"],
            time_range={"start_date": "2024-04-27", "end_date": "2024-05-27", "relative": "last_30_days"},
        )
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract(
            "What is the MRR for the last 30 days?", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS
        )
        assert intent.time_range is not None
        assert intent.time_range.relative == "last_30_days"
        assert intent.time_range.start_date
        assert intent.time_range.end_date

    def test_extract_no_time_range_returns_none(self) -> None:
        """A query without a time range should produce time_range=None."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(metrics=["ltv"], dimensions=["acquisition_channel"], time_range=None)
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract(
            "What is the lifetime value by acquisition channel?",
            AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS,
        )
        assert intent.time_range is None

    def test_relative_time_range_with_empty_dates_is_resolved(self) -> None:
        """LLM returns empty start/end dates with a relative label → extractor must fill them in."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(
            metrics=["mrr"],
            time_range={"start_date": "", "end_date": "", "relative": "last_30_days"},
        )
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract("MRR last 30 days", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert intent.time_range is not None
        # start_date should have been resolved from the relative label
        assert intent.time_range.start_date != ""


class TestFilterExtraction:
    def test_extract_single_filter(self) -> None:
        """A filter on plan_type should be extracted as a FilterClause."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(
            metrics=["mrr"],
            dimensions=["plan_type"],
            filters=[{"column": "plan_type", "operator": "eq", "value": "Enterprise"}],
        )
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract(
            "MRR for Enterprise plan", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS
        )
        assert len(intent.filters) == 1
        assert intent.filters[0].column == "plan_type"
        assert intent.filters[0].operator == "eq"
        assert intent.filters[0].value == "Enterprise"

    def test_extract_no_filters(self) -> None:
        """Query without explicit filter should produce an empty filters list."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(metrics=["mrr"], filters=[])
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(mock_json)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract("Show me total MRR", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert intent.filters == []


class TestFilterValueCoercion:
    """
    A multi-value filter must arrive as a list.

    Every SQL path branches on isinstance(value, list), so a multi-value filter
    left as a single string renders as ONE literal: IN ('basic,standard'). That is
    valid SQL matching nothing, so the query returns zero rows with
    status=success, no error is raised, and the empty result is cached.

    Measured against the live model before the fix: "MRR for basic and standard
    plans this year", "churn rate by country for US, GB and DE in 2025" and
    "total revenue for premium and basic plans last 6 months" all returned 0 rows
    where the correct answers were 2, 3 and 2 rows.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "basic,standard",
            "basic, standard",
            "  basic ,  standard  ",
            "(basic, standard)",
            "'basic', 'standard'",
            '"basic", "standard"',
            "['basic', 'standard']",
            '["basic", "standard"]',
        ],
        ids=[
            "bare", "bare-spaced", "bare-padded", "parenthesised",
            "single-quoted", "double-quoted", "bracketed", "bracketed-json",
        ],
    )
    def test_in_operator_yields_a_real_list(self, raw: str) -> None:
        f = FilterClause(column="subscription__plan_type", operator="in", value=raw)
        assert f.value == ["basic", "standard"], f"{raw!r} -> {f.value!r}"

    def test_three_values_all_survive(self) -> None:
        f = FilterClause(column="subscriber__country", operator="in", value="US,GB,DE")
        assert f.value == ["US", "GB", "DE"]

    def test_single_value_stays_a_string(self) -> None:
        """IN ('premium') is already correct — do not wrap it in a list."""
        f = FilterClause(column="subscription__plan_type", operator="in", value="premium")
        assert f.value == "premium"

    def test_eq_with_a_comma_is_not_split(self) -> None:
        """
        The load-bearing restriction. A comma inside an eq value is part of the
        value, so splitting on it would corrupt a legitimate single-value filter.
        """
        f = FilterClause(column="subscriber__country", operator="eq", value="Smith, John")
        assert f.value == "Smith, John"

    @pytest.mark.parametrize("operator", ["eq", "neq", "gt", "gte", "lt", "lte"])
    def test_non_in_operators_are_untouched(self, operator: str) -> None:
        f = FilterClause(column="c", operator=operator, value="a,b")
        assert f.value == "a,b"

    def test_bracketed_value_still_parses_for_non_in_operators(self) -> None:
        """Pre-existing behaviour: a bracketed string is never a scalar."""
        f = FilterClause(column="c", operator="eq", value="['a', 'b']")
        assert f.value == ["a", "b"]

    def test_real_list_passes_through(self) -> None:
        f = FilterClause(column="c", operator="in", value=["basic", "standard"])
        assert f.value == ["basic", "standard"]

    def test_malformed_brackets_fall_through_to_the_split(self) -> None:
        f = FilterClause(column="c", operator="in", value="[basic, standard]")
        assert f.value == ["basic", "standard"]

    def test_empty_string_is_left_alone(self) -> None:
        f = FilterClause(column="c", operator="in", value="")
        assert f.value == ""


class TestFallbackChain:
    def test_falls_back_to_secondary_when_primary_fails(self) -> None:
        """If primary LLM raises, the fallback should be tried and succeed."""
        extractor = _make_extractor()
        mock_json = _valid_intent_json(metrics=["mrr"])

        # They are distinct mock objects because we replace them after creation
        extractor._primary_client = MagicMock()
        extractor._fallback_client = MagicMock()
        extractor._tertiary_client = None

        extractor._primary_client.chat.completions.create.side_effect = RuntimeError("primary unavailable")
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(mock_json)

        intent = extractor.extract("Show me MRR", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert "mrr" in intent.metrics

    def test_all_clients_failing_raises_intent_extraction_error(self) -> None:
        """If all three clients fail, IntentExtractionError should be raised."""
        extractor = _make_extractor()
        extractor._primary_client = MagicMock()
        extractor._fallback_client = MagicMock()
        extractor._tertiary_client = None

        extractor._primary_client.chat.completions.create.side_effect = RuntimeError("p fail")
        extractor._fallback_client.chat.completions.create.side_effect = RuntimeError("f fail")

        with pytest.raises(IntentExtractionError):
            extractor.extract("Show me MRR", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)


class TestInvalidJsonRaises:
    def test_malformed_json_raises_intent_extraction_error(self) -> None:
        """LLM returning non-JSON should raise IntentExtractionError."""
        extractor = _make_extractor()
        malformed = "I cannot understand this query. Please try again."
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(malformed)
        extractor._fallback_client.chat.completions.create.return_value = _mock_llm_response(malformed)

        with pytest.raises(IntentExtractionError) as exc_info:
            extractor.extract("some query", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)

        assert exc_info.value.raw_response  # raw response preserved for debugging

    def test_openai_api_failure_raises_intent_extraction_error(self) -> None:
        """All clients raising should be wrapped in IntentExtractionError."""
        extractor = _make_extractor()
        extractor._primary_client.chat.completions.create.side_effect = Exception("Connection timeout")
        extractor._fallback_client.chat.completions.create.side_effect = Exception("Connection timeout")
        extractor._tertiary_client = None

        with pytest.raises(IntentExtractionError):
            extractor.extract("some query", AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)


class TestConversationHistory:
    """
    Blank turns must never reach the LLM.

    The dashboard chat stored its answers on `raw` and sent `content: ''`, so every
    assistant turn arrived empty — the model saw the user's prior questions but none
    of its own answers, and could not anchor on a metric it had already chosen.
    The frontend now populates `content`; this is the gateway-side guarantee that no
    other caller can reintroduce it.
    """

    @staticmethod
    def _sent_messages(extractor) -> list[dict]:
        return extractor._primary_client.chat.completions.create.call_args.kwargs["messages"]

    @staticmethod
    def _run(extractor, history):
        extractor._primary_client.chat.completions.create.return_value = _mock_llm_response(
            _valid_intent_json()
        )
        extractor.extract(
            "Show churn by plan type for 2025",
            AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS,
            history=history,
        )

    def test_blank_agent_turns_are_dropped(self) -> None:
        extractor = _make_extractor()
        self._run(extractor, [
            SimpleNamespace(role="user", content="Show churn by plan type for 2026"),
            SimpleNamespace(role="agent", content=""),
        ])
        sent = self._sent_messages(extractor)
        assert all(m["content"].strip() for m in sent), "a blank turn reached the LLM"
        assert any("2026" in m["content"] for m in sent), "real user turn was lost"

    def test_whitespace_only_turns_are_dropped(self) -> None:
        extractor = _make_extractor()
        self._run(extractor, [SimpleNamespace(role="agent", content="   \n  ")])
        assert all(m["content"].strip() for m in self._sent_messages(extractor))

    def test_populated_agent_turns_survive_as_assistant(self) -> None:
        extractor = _make_extractor()
        self._run(extractor, [
            SimpleNamespace(role="user", content="Show churn by plan type for 2026"),
            SimpleNamespace(role="agent", content="Churn rate was highest on basic."),
        ])
        sent = self._sent_messages(extractor)
        assistant = [m for m in sent if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert "highest on basic" in assistant[0]["content"]

    def test_truncation_counts_only_non_empty_turns(self) -> None:
        """Blanks must not consume the 5-turn budget that real turns need."""
        extractor = _make_extractor()
        history = []
        for i in range(6):
            history.append(SimpleNamespace(role="user", content=f"question {i}"))
            history.append(SimpleNamespace(role="agent", content=""))
        self._run(extractor, history)
        sent = self._sent_messages(extractor)
        # system + 5 surviving user turns + the current question
        assert len([m for m in sent if m["content"].startswith("question ")]) == 5
        assert any("question 5" in m["content"] for m in sent), "newest turn dropped"

    def test_no_history_sends_only_system_and_query(self) -> None:
        extractor = _make_extractor()
        self._run(extractor, [])
        sent = self._sent_messages(extractor)
        assert len(sent) == 2
        assert sent[0]["role"] == "system"


class TestBuildSystemPrompt:
    def test_system_prompt_contains_metrics(self) -> None:
        """System prompt must list all available metrics."""
        extractor = _make_extractor()
        prompt = extractor.build_system_prompt(AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        for metric in AVAILABLE_METRICS:
            assert metric in prompt, f"Metric '{metric}' missing from system prompt."

    def test_system_prompt_contains_critical_rules(self) -> None:
        """System prompt must contain CRITICAL RULES section and reference JSON."""
        extractor = _make_extractor()
        prompt = extractor.build_system_prompt(AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert "CRITICAL RULES" in prompt or "critical" in prompt.lower()
        assert "JSON" in prompt or "json" in prompt.lower()

    def test_system_prompt_contains_dimensions(self) -> None:
        """System prompt must include dimension lists for each metric."""
        extractor = _make_extractor()
        prompt = extractor.build_system_prompt(AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert "plan_type" in prompt
        assert "acquisition_channel" in prompt

    def test_system_prompt_is_non_empty_string(self) -> None:
        """Prompt must be a non-empty string."""
        extractor = _make_extractor()
        prompt = extractor.build_system_prompt(AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert isinstance(prompt, str)
        assert len(prompt) > 100  # sanity: should be substantial

    def test_system_prompt_disambiguates_bare_churn(self) -> None:
        """
        A bare "churn" must be pinned to churn_rate in the prompt.

        Without this, "Show churn by plan type" resolved to churned_subscribers on
        one run and churn_rate on the next, returning two contradictory answers
        from the same question (production incident, 2026-07-30).
        """
        extractor = _make_extractor()
        prompt = extractor.build_system_prompt(AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert "METRIC DISAMBIGUATION" in prompt
        assert "RATE BEATS COUNT" in prompt
        # The bare-churn worked example must be present, not just the rule.
        assert "Show churn by plan type" in prompt

    def test_system_prompt_keeps_count_path_for_explicit_count(self) -> None:
        """The disambiguation rule must not collapse every churn query to a rate."""
        extractor = _make_extractor()
        prompt = extractor.build_system_prompt(AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)
        assert "churned_subscribers" in prompt
        assert "how many" in prompt.lower()


class TestPromptDoesNotShipRegistryInternals:
    """
    The prompt used to carry every field of every MetricDefinition, because
    `metrics_section` was `f"  - {m}"` on the object itself — a pydantic repr, not
    a chosen serialisation. That shipped each metric's `raw_yaml` (its raw dbt
    source, 6,010 chars) and `lineage` (its raw->stg->int->mart chain, 5,770) on
    EVERY request: 11,780 chars, 27% of the prompt, none of it useful for choosing
    between two metrics. Lineage is Stage 7 response metadata that leaked into the
    Stage 1 prompt.

    It surfaced as a provider failure, not as a cost problem — Groq's free tier
    caps a request at 8,000 tokens and the prompt needed ~11,150, so the whole
    fallback rung was structurally unusable.

    These tests exist because the way those fields got in was an accident of
    string interpolation, so the same slip would reintroduce all of them at once.
    """

    def _metric_objects(self):
        """MetricDefinition-shaped objects, as the route passes them."""
        return [
            SimpleNamespace(
                name=name,
                label=f"{name.title()} Label",
                description=f"Description of {name}.",
                metric_type="ratio",
                raw_yaml="name: " + name + "\nlabel: should never reach the prompt\n",
                lineage=["raw.subscribers", "stg_subscribers", "fct_mrr_monthly"],
                source_model="fct_mrr_monthly",
                measure_column="mrr_usd",
                fanout_risk_models=["int_subscription_periods"],
            )
            for name in AVAILABLE_METRICS
        ]

    def test_raw_yaml_and_lineage_never_reach_the_prompt(self) -> None:
        prompt = _make_extractor().build_system_prompt(
            self._metric_objects(), AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS
        )
        assert "should never reach the prompt" not in prompt, "raw_yaml leaked"
        assert "raw_yaml=" not in prompt
        assert "lineage=" not in prompt
        assert "stg_subscribers" not in prompt, "lineage chain leaked"
        assert "fanout_risk_models=" not in prompt
        assert "measure_column=" not in prompt

    def test_what_the_model_needs_is_still_there(self) -> None:
        """Trimming must not cost the fields that drive metric selection."""
        prompt = _make_extractor().build_system_prompt(
            self._metric_objects(), AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS
        )
        for name in AVAILABLE_METRICS:
            assert name in prompt
            assert f"Description of {name}." in prompt, f"{name} lost its description"

    def test_object_and_string_callers_both_get_dimensions_and_grains(self) -> None:
        """
        The keying bug. `available_metrics` arrives as OBJECTS from the route and
        as NAMES from the eval harness (run_evals.py:176), while the dimension and
        grain maps are keyed by name. `if k in selected_metrics` therefore matched
        0 of 23 keys on the route path, and the CERTIFIED DIMENSIONS MAP and TIME
        GRANULARITIES sections rendered EMPTY in production.

        Nothing failed loudly: the model still saw dimension names as a side
        effect of the repr above, so it learned BARE names and never the qualified
        `subscriber__country` form that build_dimension_prefix_map() produces and
        the compiler resolves. Removing the repr without fixing this would have
        left the route with no dimension vocabulary at all.
        """
        extractor = _make_extractor()
        for label, metrics in (("objects", self._metric_objects()),
                               ("strings", AVAILABLE_METRICS)):
            prompt = extractor.build_system_prompt(
                metrics, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS
            )
            assert "billing_cycle" in prompt, f"{label}: dimensions map is empty"
            assert "quarter" in prompt, f"{label}: time granularities are empty"

    def test_max_tokens_is_a_reservation_sized_to_the_real_output(self) -> None:
        """
        `max_tokens` is an output RESERVATION, and providers gate on it before
        generating anything: OpenRouter refused a live request for "up to 1024
        tokens" when it could afford 434, for an answer that needed ~145.
        Measured completions are 141-148 tokens.
        """
        from core.intent_extractor import _INTENT_MAX_TOKENS

        assert _INTENT_MAX_TOKENS >= 250, "too tight — would truncate the intent JSON"
        assert _INTENT_MAX_TOKENS <= 512, "over-reserving is what triggered the 402"

    def test_a_plain_string_caller_still_renders(self) -> None:
        """Names-only callers must not crash on the attribute lookups."""
        prompt = _make_extractor().build_system_prompt(
            AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS
        )
        for name in AVAILABLE_METRICS:
            assert name in prompt


class TestPrimaryRungSurvivesATransientFailure:
    """
    Both production outages were the SAME shape: the primary timed out at exactly
    the 15 s ceiling, then Groq 404'd (retired model) and OpenRouter 402'd (no
    credit), and the user got a 400 after ~15.9 s.

    The 15 s ceiling was chosen so a struggling primary reached the fallback chain
    quickly. That reasoning inverted when the chain stopped working: with two dead
    rungs below it, failing fast converts a slow SUCCESS into a hard failure and
    gains nothing. Measured median for the real 9,912-token prompt is 4.55 s
    (range 2.81-5.16), so 40 s is generous without being reckless — a healthy call
    still returns in ~5 s.
    """

    def test_the_timeout_comes_from_settings_and_is_generous(self) -> None:
        from config import settings

        assert settings.llm_timeout_seconds >= 30, (
            "measured p50 is 4.55s but production hit 15s twice; a tight ceiling "
            "only helps if there is a working fallback to reach"
        )

    @pytest.mark.parametrize("message,status", [
        ("Request timed out.", None),
        ("503 This model is currently experiencing high demand", None),
        ("quota exceeded RESOURCE_EXHAUSTED", None),
        ("Rate limit reached for model", 429),
        ("internal server error", 500),
    ])
    def test_transient_failures_are_retried(self, message, status) -> None:
        from core.intent_extractor import _is_transient_llm_error

        exc = Exception(message)
        if status is not None:
            exc.status_code = status
        assert _is_transient_llm_error(exc), f"{message!r} should be retried"

    @pytest.mark.parametrize("message,status", [
        ("The model `llama-3.1-8b-instant` does not exist", 404),
        ("This request requires more credits, or fewer max_tokens", 402),
        ("Prompt tokens limit exceeded: 9678 > 3621", 402),
        ("Failed to validate JSON. Please adjust your prompt.", 400),
        ("Incorrect API key provided", 401),
    ])
    def test_deterministic_failures_are_not_retried(self, message, status) -> None:
        """
        Every one of these is a real error from the logs. Retrying them reaches a
        byte-identical verdict while spending the timeout budget, which is why the
        transient list is CLOSED rather than allow-by-default.
        """
        from core.intent_extractor import _is_transient_llm_error

        exc = Exception(message)
        exc.status_code = status
        assert not _is_transient_llm_error(exc), f"{message!r} must not be retried"

    @staticmethod
    def _distinct_rungs(extractor):
        """Give each rung its OWN mock.

        `patch("core.intent_extractor.OpenAI")` hands back the same
        `MagicMock.return_value` for every construction, so `_primary_client` and
        `_fallback_client` are literally the same object — a side_effect set on one
        silently overwrites the other, and call_count cannot tell which rung ran.
        """
        extractor._primary_client = MagicMock()
        extractor._fallback_client = MagicMock()
        extractor._tertiary_client = None
        return extractor

    @staticmethod
    def _ok(metric: str):
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content=(
            '{"query_type":"metric_query","metrics":["' + metric + '"],'
            '"dimensions":[],"filters":[],"time_range":null,'
            '"needs_clarification":false}'
        )))]
        return response

    def test_a_transient_primary_failure_does_not_reach_the_fallbacks(self) -> None:
        """The whole point: one retry on the rung that works, before giving up."""
        extractor = self._distinct_rungs(_make_extractor())
        extractor._primary_client.chat.completions.create.side_effect = [
            Exception("Request timed out."), self._ok("mrr"),
        ]

        intent = extractor.extract("what is mrr", AVAILABLE_METRICS,
                                   AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)

        assert intent.metrics == ["mrr"]
        assert extractor._primary_client.chat.completions.create.call_count == 2
        assert extractor._fallback_client.chat.completions.create.call_count == 0, (
            "the primary's retry succeeded — the dead rungs must not be touched"
        )

    def test_a_deterministic_primary_failure_falls_through_immediately(self) -> None:
        """A retired model or a bad key must not cost a second timeout."""
        extractor = self._distinct_rungs(_make_extractor())
        exc = Exception("The model does not exist or you do not have access to it")
        exc.status_code = 404
        extractor._primary_client.chat.completions.create.side_effect = exc
        extractor._fallback_client.chat.completions.create.return_value = self._ok("ltv")

        intent = extractor.extract("what is ltv", AVAILABLE_METRICS,
                                   AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS)

        assert intent.metrics == ["ltv"]
        assert extractor._primary_client.chat.completions.create.call_count == 1, (
            "a 404 was retried — that wastes the timeout budget for an identical result"
        )


class TestOneObjectNotAnArray:
    """
    `multi-metric-001` ("Show me MRR and churn rate by country for last quarter")
    failed persistently, and the reported error was a red herring:

        LLM returned non-JSON response: Unterminated string ... (char 750)

    That reads like a truncation / max_tokens problem. It was not. The model was
    answering a two-metric question with a JSON ARRAY of two objects, one per
    metric, which is ~2x the length of the real schema and so ran into the output
    ceiling. Gemini returns a steady 154 tokens for this query in the correct
    single-object shape, measured five times.

    The truncation was the visible symptom of a WRONG SHAPE, and the shape is the
    dangerous half: an array that fits under the ceiling parses fine, and reading
    the first element silently drops `churn_rate` — a confident wrong answer
    instead of an error. Raising max_tokens would have hidden that.
    """

    def test_the_prompt_forbids_an_array_of_objects(self) -> None:
        prompt = _make_extractor().build_system_prompt(
            AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS
        )
        assert "never a JSON array" in prompt
        assert "ONE JSON object" in prompt

    def test_it_shows_the_multi_metric_shape_both_ways(self) -> None:
        """
        A bare prohibition is weaker than a contrasting pair: the model has to see
        that two metrics go in one `metrics` list, not into two objects.
        """
        prompt = _make_extractor().build_system_prompt(
            AVAILABLE_METRICS, AVAILABLE_DIMENSIONS, AVAILABLE_TIME_GRAINS
        )
        assert '"metrics": ["mrr", "churn_rate"]' in prompt, "no correct example"
        assert "one object per metric" in prompt, "no counter-example"
