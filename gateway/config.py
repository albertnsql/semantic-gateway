"""
config.py — Gateway settings loaded via pydantic-settings from .env file.

Single source of truth for all environment-dependent configuration.
Loaded once at startup; injected into services via dependency injection.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All gateway configuration.  Values are read from environment variables
    (or the .env file in the same directory as the gateway root).

    Pydantic-settings automatically coerces types and raises a descriptive
    ValidationError if a required field is missing.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ LLM Providers
    # Primary: OpenRouter
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-2.5-flash"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Fallback: Groq (via OpenAI compat)
    openai_api_key: str
    openai_model: str = "llama-3.1-8b-instant"  # Cheaper Groq model
    openai_temperature: float = 0.0  # deterministic for analytics
    llm_base_url: str = "https://api.groq.com/openai/v1"

    # Tertiary: Google Gemini (via OpenAI compat)
    google_api_key: str = ""
    google_model: str = "gemini-3.1-flash-lite"
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

    # --------------------------------------------------------------- Snowflake
    snowflake_account: str = "your-account.snowflakecomputing.com"
    snowflake_user: str = "snowflake_user"
    snowflake_password: str = "snowflake_password"
    snowflake_database: str = "streaming_analytics"
    snowflake_warehouse: str = "compute_wh"
    snowflake_role: str = "transformer"
    snowflake_schema: str = "marts"

    # ------------------------------------------------------------ dbt / MetricFlow
    manifest_path: str = (
        "../dbt_streaming_analytics/streaming_analytics/target/manifest.json"
    )
    metrics_path: str = "../dbt_streaming_analytics/streaming_analytics/metrics"
    semantic_models_path: str = (
        "../dbt_streaming_analytics/streaming_analytics/models/semantic"
    )

    # ----------------------------------------------------------------- Runtime
    gateway_env: str = "development"
    log_level: str = "INFO"
    gateway_version: str = "1.0.0"
    cache_ttl_seconds: int = 28800  # Intent-keyed result cache TTL (default: 8 hours)
    sql_template_cache_ttl_seconds: int = 86400  # Compiled SQL template TTL (default: 24 h)
    # Templates change only when the dbt semantic model is redeployed (gateway restart),
    # so a longer TTL is safe and avoids re-running MetricFlow unnecessarily.

    # --------------------------------------------------------- Memory / capacity
    # These caps protect against OOM on memory-constrained hosts (e.g. Render free tier
    # which provides 512 MB RAM). Tune them via environment variables:
    #
    #   QUERY_CACHE_MAXSIZE=100        (full-tier default: 500)
    #   SQL_TEMPLATE_CACHE_MAXSIZE=50  (full-tier default: 200)
    #
    # Rule of thumb for Render free tier:
    #   query_cache_maxsize   ≤ 100  (each entry ~8 KB JSON payload)
    #   sql_template_cache_maxsize ≤ 50  (each entry ~4 KB SQL string)
    query_cache_maxsize: int = 500           # lower to 100 on 512 MB hosts
    sql_template_cache_maxsize: int = 200    # lower to 50  on 512 MB hosts
    
    warmup_matrix: dict[str, list[str]] = {
        # MetricFlow-validated dimension names only.
        # Prefixes must match the entity defined in the semantic model:
        #   subscription__ → fct_mrr_monthly / subscription entity
        #   subscriber__   → dim_subscribers / subscriber entity
        #   session__      → fct_stream_sessions / session entity
        #   event__        → stg_recommendation_events / event entity
        #   payment__      → fct_payments / payment entity
        #
        # ⚠ On Render free tier (512 MB RAM): keep this to ≤ 6 total combinations.
        # Each warm-up entry forks a MetricFlow subprocess (~80 MB spike each).
        # The entries below (9 combos) are safe for 1 GB+ hosts. Trim to the
        # commented-out subset for 512 MB:
        #
        # Render-safe subset (6 combos — comment the rest out):
        #   "mrr":               ["subscription__plan_type", "subscriber__country"]
        #   "total_subscribers": ["subscriber__plan_type"]
        #   "churn_rate":        ["subscriber__plan_type"]
        #
        # ── CONFIRMED MetricFlow-native metrics (semantic manifest validates these) ──
        "mrr":                   ["subscription__plan_type", "subscriber__country", "subscriber__cohort_month"],
        "total_subscribers":     ["subscriber__plan_type", "subscriber__country", "subscriber__acquisition_channel"],
        # churn_rate lives on fct_mrr_monthly (monthly event-based definition):
        # subscription__plan_type is native; subscriber__ dims join via the subscriber entity.
        "churn_rate":            ["subscription__plan_type", "subscriber__country", "subscriber__churn_reason"],
        "churned_subscribers":   ["subscriber__plan_type", "subscriber__country"],
        "ltv":                   ["payment__payment_method", "subscriber__plan_type", "subscriber__country"],
        # total_revenue's measure is mrr_usd on sem_mrr — payment__ dims are NOT
        # reachable (MetricFlow rejects them; confirmed via precompile failures).
        "total_revenue":         ["subscription__plan_type", "subscriber__plan_type", "subscriber__country"],
        "expansion_mrr":         ["subscription__plan_type", "subscriber__country"],
        # engagement_rate joins fct_stream_sessions → session entity; plan_type is on subscriber entity
        "engagement_rate":       ["session__device_type", "subscriber__plan_type", "subscriber__country"],
        "recommendation_ctr":    ["event__recommendation_type"],
        "total_recommendations": ["event__recommendation_type"],
        "clicked_recommendations": ["event__recommendation_type"],
        "avg_watch_time":        ["session__device_type", "subscriber__plan_type", "subscriber__country"],
        "total_sessions":        ["session__device_type", "subscriber__plan_type", "subscriber__country"],
        #
        # ── REMOVED / NOT in MetricFlow semantic manifest ──────────────────────────
        # "new_subscribers" — MetricFlow rejects this metric name every time.
        # It is handled by the governed LLM fallback SQL path at query time.
        # Adding it here wastes ~35s per combination and pollutes subprocess output.
        # If you add new_subscribers to the dbt semantic model, re-enable it:
        #   "new_subscribers": ["subscriber__plan_type", "subscriber__acquisition_channel"],
    }

    # Admin secret key — required to call POST /api/v1/cache/clear in production.
    # Leave empty ("") to allow unauthenticated access in development only.
    admin_secret_key: str = ""


# Module-level singleton — importable everywhere without re-parsing .env
settings = Settings()
