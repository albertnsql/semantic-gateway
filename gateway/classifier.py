"""
gateway/classifier.py — Two-stage intent classifier for incoming analytics questions.

Stage 1: classify the question as METRIC_QUERY, SCHEMA_QUESTION, or OUT_OF_SCOPE
         before attempting semantic extraction.  This prevents unanswerable questions
         from ever reaching the Snowflake execution layer.

Reuses the same LLM client chain (primary→fallback→tertiary) configured in
IntentExtractor so no new API credentials are needed.

Usage::

    classifier = IntentClassifier(llm_client=extractor._primary_client,
                                   model=settings.google_model)
    result = classifier.classify("Why did MRR drop last quarter?")
    # → {"query_type": QueryType.OUT_OF_SCOPE, "confidence": 0.95, "reason": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from enum import Enum

logger = logging.getLogger(__name__)


class QueryType(Enum):
    """Enumeration of the three intent classification categories."""
    METRIC_QUERY    = "metric_query"
    SCHEMA_QUESTION = "schema_question"
    OUT_OF_SCOPE    = "out_of_scope"


class IntentClassifier:
    """Classifies incoming questions before they enter the full pipeline."""

    SYSTEM_PROMPT = """
You are a query router for an analytics system. 
Classify the user's question into exactly one of these categories:

METRIC_QUERY: The question asks for specific data, numbers, or metrics 
that can be answered by querying a data warehouse. 
Examples: "Show me MRR by segment", "What was churn last month?",
"Compare revenue across regions"

SCHEMA_QUESTION: The question asks about what data or metrics exist, 
what dimensions are available, or how the system works.
Examples: "What metrics do you have?", "What dimensions can I filter by?",
"What does MRR mean in this system?"

OUT_OF_SCOPE: The question asks for reasoning, causation, predictions, 
or anything that cannot be answered by a SQL query.
Examples: "Why did MRR drop?", "What should I focus on?", 
"Predict next quarter's revenue", "How can I improve retention?"

Respond with ONLY a JSON object, no other text:
{
  "query_type": "metric_query" | "schema_question" | "out_of_scope",
  "confidence": 0.0-1.0,
  "reason": "one sentence explanation"
}
"""

    # Maximum seconds we will sleep while respecting a Retry-After hint.
    # Keeps individual requests from stalling the gateway for too long.
    _MAX_RETRY_SLEEP_S: float = 30.0

    def __init__(self, llm_client, model: str) -> None:
        """Store the LLM client and model name for classification calls."""
        self._client = llm_client
        self._model  = model

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_retry_after(exc: Exception) -> float | None:
        """
        Try to parse the recommended retry-after delay (in seconds) from a
        rate-limit exception message.

        Gemini / Google AI Studio 429 errors embed a hint like:
            "Please retry in 25.161069634s."
        We extract that number and return it, capped at _MAX_RETRY_SLEEP_S.
        Returns None if no delay could be parsed.
        """
        msg = str(exc)
        # Pattern covers both "25.16s" and "25s" formats
        match = re.search(r"retry in\s+([\d.]+)\s*s", msg, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
        return None

    async def _sleep_retry_after(self, exc: Exception) -> None:
        """
        If the exception carries a Retry-After hint, sleep for that duration
        (capped at _MAX_RETRY_SLEEP_S) so the next request doesn't immediately
        hit the same rate-limit wall.

        Uses asyncio.sleep() so the Uvicorn event loop is never blocked —
        other requests continue to be served during the back-off window.
        """
        delay = self._extract_retry_after(exc)
        if delay is not None:
            capped = min(delay, self._MAX_RETRY_SLEEP_S)
            logger.info(
                "IntentClassifier: 429 rate-limit detected — sleeping %.1f s "
                "(retry-after=%.1f s, cap=%.0f s).",
                capped, delay, self._MAX_RETRY_SLEEP_S,
            )
            await asyncio.sleep(capped)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def classify(self, question: str) -> dict:
        """
        Classify a question into METRIC_QUERY, SCHEMA_QUESTION, or OUT_OF_SCOPE.

        This method is async so that any 429 rate-limit back-off uses
        asyncio.sleep() rather than time.sleep(), keeping the Uvicorn event
        loop free to serve other requests during the wait.
        """
        _DEFAULT = {
            "query_type": QueryType.METRIC_QUERY,
            "confidence": 0.5,
            "reason": "classification failed, defaulting to metric query",
        }
        if not self._client:
            logger.warning("IntentClassifier has no LLM client — defaulting to METRIC_QUERY.")
            return _DEFAULT

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT.strip()},
                    {"role": "user", "content": question},
                ],
                temperature=0,
                max_tokens=150,
            )
            raw = (response.choices[0].message.content or "").strip()
            # Strip markdown fences if the model ignores instructions
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw)
            qt_str = parsed.get("query_type", "metric_query")
            try:
                qt = QueryType(qt_str)
            except ValueError:
                qt = QueryType.METRIC_QUERY
            return {
                "query_type": qt,
                "confidence": float(parsed.get("confidence", 0.5)),
                "reason": str(parsed.get("reason", "")),
            }
        except Exception as exc:
            logger.warning("IntentClassifier LLM call failed (%s) — defaulting to METRIC_QUERY.", exc)
            # Honour the Retry-After delay so the very next request doesn't
            # immediately slam into the same rate-limit wall.
            # Uses asyncio.sleep() — does NOT block the event loop.
            await self._sleep_retry_after(exc)
            return _DEFAULT


# ──────────────────────────────────────────── Response templates

SCHEMA_RESPONSE_TEMPLATE = """I can help you understand what's available in this system.
Here are the metrics you can query: {metric_names}

For each metric, you can filter and group by various dimensions.
Try asking something like: "Show me {example_metric} by {example_dimension}"
"""

OUT_OF_SCOPE_RESPONSE_TEMPLATE = """That's a great question, but it requires reasoning about causes and context \
that goes beyond what I can answer by querying data directly.

What I can tell you is the data behind it — for example:
"{suggested_query}"

Would you like me to run that instead?
"""


def build_out_of_scope_suggestion(question: str, available_metrics: list) -> str:
    """Return a suggested answerable query rephrasing for an out-of-scope question."""
    if not available_metrics:
        return "Show me the latest data"
    q_lower = question.lower()
    # Try to match a metric name mentioned in the question
    for metric in available_metrics:
        if metric.lower().replace("_", " ") in q_lower or metric.lower() in q_lower:
            return f"Show me {metric} trend over the last 6 months"
    # Fall back to the first available metric
    return f"Show me {available_metrics[0]} trend over the last 6 months"
