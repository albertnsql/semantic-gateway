"""
core/exceptions.py — Custom exception hierarchy for the AI Semantic Gateway.

All domain-specific errors inherit from GatewayBaseError so callers can
catch the entire family with a single except clause when needed.
"""

from __future__ import annotations


class GatewayBaseError(Exception):
    """Base class for all AI Semantic Gateway errors."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message} | detail: {self.detail}"
        return self.message


class IntentExtractionError(GatewayBaseError):
    """
    Raised when the OpenAI call succeeds but the response cannot be parsed
    into a valid QueryIntent.  The raw LLM response is preserved in
    ``self.raw_response`` for debugging.
    """

    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message, detail=raw_response[:500] if raw_response else None)
        self.raw_response = raw_response


class SemanticValidationError(GatewayBaseError):
    """
    Raised when the SemanticValidator encounters an unrecoverable internal
    error (distinct from a *business-rule* violation, which is expressed via
    ValidationResult.violations).
    """


class SQLGenerationError(GatewayBaseError):
    """
    Raised when MetricFlow CLI invocation fails or returns unparseable output.
    """

    def __init__(self, message: str, mf_command: str = "", stderr: str = "") -> None:
        detail = f"command={mf_command!r} stderr={stderr[:300]!r}" if mf_command else stderr
        super().__init__(message, detail=detail)
        self.mf_command = mf_command
        self.stderr = stderr


class MetricNotFoundError(GatewayBaseError):
    """Raised when a named metric is not present in the MetricRegistry."""

    def __init__(self, metric_name: str) -> None:
        super().__init__(
            f"Metric '{metric_name}' not found in the certified registry.",
            detail=f"metric_name={metric_name!r}",
        )
        self.metric_name = metric_name


class SnowflakeConnectionError(GatewayBaseError):
    """
    Raised when the gateway cannot establish a Snowflake connection or a
    query times out / returns a connection-level error.
    """


class ManifestLoadError(GatewayBaseError):
    """Raised when manifest.json cannot be loaded or is malformed."""


class MetricsLoadError(GatewayBaseError):
    """Raised when YAML metric/semantic files cannot be parsed."""
