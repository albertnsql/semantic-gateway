"""
tests/test_classifier.py — Unit tests for gateway/classifier.py

Covers:
  - _extract_retry_after: regex parsing of Gemini 429 messages
  - _extract_retry_after: returns None for non-rate-limit errors
  - classify: returns METRIC_QUERY default when no LLM client
  - classify: correctly parses valid LLM responses
  - classify: falls back to METRIC_QUERY on JSON parse failure
  - build_out_of_scope_suggestion: metric matching heuristic
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from classifier import IntentClassifier, QueryType, build_out_of_scope_suggestion


# ─────────────────────────────────────────── _extract_retry_after


class TestExtractRetryAfter:
    """Tests for the static retry-after delay parser."""

    def test_parses_fractional_seconds(self):
        """Typical Gemini 429 message with fractional seconds."""
        exc = Exception("Please retry in 25.161069634s after the quota resets.")
        delay = IntentClassifier._extract_retry_after(exc)
        assert delay is not None
        assert abs(delay - 25.161069634) < 1e-6

    def test_parses_whole_seconds(self):
        """Gemini message with a whole-number delay."""
        exc = Exception("Rate limited. Retry in 10s.")
        delay = IntentClassifier._extract_retry_after(exc)
        assert delay is not None
        assert delay == 10.0

    def test_parses_case_insensitive(self):
        """'Retry In' with mixed case should still match."""
        exc = Exception("RETRY IN 5.5s once quota is free.")
        delay = IntentClassifier._extract_retry_after(exc)
        assert delay is not None
        assert abs(delay - 5.5) < 1e-6

    def test_returns_none_for_non_429(self):
        """Non-rate-limit errors must return None."""
        exc = Exception("Connection refused: timeout after 30 s of waiting.")
        delay = IntentClassifier._extract_retry_after(exc)
        assert delay is None

    def test_returns_none_for_empty_message(self):
        """Empty exception message must return None, not crash."""
        exc = Exception("")
        assert IntentClassifier._extract_retry_after(exc) is None

    def test_returns_none_for_generic_500(self):
        """Internal server error — no retry hint — must return None."""
        exc = Exception("Internal Server Error (500).")
        assert IntentClassifier._extract_retry_after(exc) is None


# ─────────────────────────────────────────── _sleep_retry_after


class TestSleepRetryAfter:
    """Tests that _sleep_retry_after respects the cap and uses asyncio.sleep."""

    @pytest.mark.asyncio
    async def test_sleeps_for_detected_delay(self):
        clf = IntentClassifier(llm_client=None, model="x")
        exc = Exception("retry in 5s")
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await clf._sleep_retry_after(exc)
        mock_sleep.assert_called_once_with(5.0)

    @pytest.mark.asyncio
    async def test_cap_is_respected(self):
        """Delays longer than _MAX_RETRY_SLEEP_S should be capped."""
        clf = IntentClassifier(llm_client=None, model="x")
        exc = Exception("retry in 999s")
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await clf._sleep_retry_after(exc)
        called_with = mock_sleep.call_args[0][0]
        assert called_with == IntentClassifier._MAX_RETRY_SLEEP_S

    @pytest.mark.asyncio
    async def test_no_sleep_when_no_retry_hint(self):
        """If no retry hint is present, asyncio.sleep should not be called."""
        clf = IntentClassifier(llm_client=None, model="x")
        exc = Exception("Some other error with no retry info")
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await clf._sleep_retry_after(exc)
        mock_sleep.assert_not_called()


# ─────────────────────────────────────────── classify


class TestClassify:
    """Tests for IntentClassifier.classify() — now async."""

    @pytest.mark.asyncio
    async def test_defaults_to_metric_query_when_no_client(self):
        """No LLM client → always returns METRIC_QUERY without crashing."""
        clf = IntentClassifier(llm_client=None, model="any-model")
        result = await clf.classify("Why did MRR drop?")
        assert result["query_type"] == QueryType.METRIC_QUERY

    @pytest.mark.asyncio
    async def test_parses_metric_query_response(self):
        """LLM returns a valid metric_query JSON → correctly parsed."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "query_type": "metric_query",
                            "confidence": 0.95,
                            "reason": "Asks for data."
                        })
                    )
                )
            ]
        )
        clf = IntentClassifier(llm_client=mock_client, model="test-model")
        result = await clf.classify("Show me MRR by plan type")
        assert result["query_type"] == QueryType.METRIC_QUERY
        assert result["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_parses_schema_question_response(self):
        """LLM returns schema_question → correctly mapped."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "query_type": "schema_question",
                            "confidence": 0.9,
                            "reason": "Asks about available metrics."
                        })
                    )
                )
            ]
        )
        clf = IntentClassifier(llm_client=mock_client, model="test-model")
        result = await clf.classify("What metrics do you have?")
        assert result["query_type"] == QueryType.SCHEMA_QUESTION

    @pytest.mark.asyncio
    async def test_parses_out_of_scope_response(self):
        """LLM returns out_of_scope → correctly mapped."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "query_type": "out_of_scope",
                            "confidence": 0.85,
                            "reason": "Asks for prediction."
                        })
                    )
                )
            ]
        )
        clf = IntentClassifier(llm_client=mock_client, model="test-model")
        result = await clf.classify("What will MRR be next quarter?")
        assert result["query_type"] == QueryType.OUT_OF_SCOPE

    @pytest.mark.asyncio
    async def test_falls_back_on_invalid_json(self):
        """LLM returns non-JSON → must default to METRIC_QUERY, not raise."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Sorry, I can't help with that.")
                )
            ]
        )
        clf = IntentClassifier(llm_client=mock_client, model="test-model")
        result = await clf.classify("anything")
        assert result["query_type"] == QueryType.METRIC_QUERY

    @pytest.mark.asyncio
    async def test_falls_back_on_unknown_query_type(self):
        """LLM returns unrecognised query_type → defaults to METRIC_QUERY."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "query_type": "totally_made_up",
                            "confidence": 0.5,
                            "reason": "???",
                        })
                    )
                )
            ]
        )
        clf = IntentClassifier(llm_client=mock_client, model="test-model")
        result = await clf.classify("test")
        assert result["query_type"] == QueryType.METRIC_QUERY

    @pytest.mark.asyncio
    async def test_falls_back_on_api_exception(self):
        """If the LLM call raises, return METRIC_QUERY default, don't propagate."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("network error")
        clf = IntentClassifier(llm_client=mock_client, model="test-model")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await clf.classify("What is MRR?")
        assert result["query_type"] == QueryType.METRIC_QUERY

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        """LLM wraps JSON in ```json ... ``` fences → still parsed correctly."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="```json\n{\"query_type\": \"metric_query\", \"confidence\": 0.8, \"reason\": \"data\"}\n```"
                    )
                )
            ]
        )
        clf = IntentClassifier(llm_client=mock_client, model="test-model")
        result = await clf.classify("Show me churn")
        assert result["query_type"] == QueryType.METRIC_QUERY

    @pytest.mark.asyncio
    async def test_no_blocking_sleep_on_rate_limit(self):
        """On 429, asyncio.sleep is awaited (not time.sleep) — event loop stays free."""
        import time
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception(
            "Error code: 429 - Please retry in 10s."
        )
        clf = IntentClassifier(llm_client=mock_client, model="test-model")
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_async_sleep, \
             patch("time.sleep") as mock_sync_sleep:
            result = await clf.classify("Show me MRR")
        # asyncio.sleep should be called, time.sleep should NOT
        mock_async_sleep.assert_called_once_with(10.0)
        mock_sync_sleep.assert_not_called()
        assert result["query_type"] == QueryType.METRIC_QUERY


# ─────────────────────────────────────────── build_out_of_scope_suggestion


class TestBuildOutOfScopeSuggestion:
    """Tests for the suggestion builder helper."""

    def test_returns_fallback_when_no_metrics(self):
        result = build_out_of_scope_suggestion("why did we lose users?", [])
        assert result == "Show me the latest data"

    def test_matches_metric_name_in_question(self):
        result = build_out_of_scope_suggestion(
            "Why did MRR drop last quarter?", ["mrr", "churn_rate"]
        )
        assert "mrr" in result.lower()

    def test_falls_back_to_first_metric_when_no_match(self):
        result = build_out_of_scope_suggestion(
            "How can I improve retention?", ["mrr", "churn_rate"]
        )
        # No metric mentioned → should use first available
        assert "mrr" in result.lower()

    def test_matches_metric_with_underscore_as_space(self):
        """'churn rate' in question should match 'churn_rate' metric."""
        result = build_out_of_scope_suggestion(
            "Why is my churn rate so high?", ["mrr", "churn_rate"]
        )
        assert "churn_rate" in result.lower()
