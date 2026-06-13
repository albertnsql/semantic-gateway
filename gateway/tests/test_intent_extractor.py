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
from core.intent_extractor import IntentExtractor, QueryIntent, TimeRange


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
