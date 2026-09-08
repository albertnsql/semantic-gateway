"""
config.py — Gateway settings loaded via pydantic-settings from .env file.

Single source of truth for all environment-dependent configuration.
Loaded once at startup; injected into services via dependency injection.
"""

from __future__ import annotations

import os
import sys
import tempfile

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
    # The SAME model as the Google rung, deliberately. OpenRouter's catalogue
    # includes google/gemini-3.1-flash-lite, so this is not a downgrade to a
    # different model — it is a second ROUTE to the same one, which is the whole
    # point: on 2026-09-08 Render reached api.groq.com (404 in ~200 ms) and
    # openrouter.ai (402 in ~400 ms) with the identical 38 KB body while
    # generativelanguage.googleapis.com returned nothing for 40 s, twice. Google is
    # not rejecting those requests (a quota breach is a fast 429, overload a fast
    # 503) — it is dropping them, which is the known hazard of a shared free-tier
    # egress IP. Was google/gemini-2.5-flash.
    openrouter_model: str = "google/gemini-3.1-flash-lite"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Fallback: Groq (via OpenAI compat)
    openai_api_key: str
    openai_model: str = "llama-3.1-8b-instant"  # Cheaper Groq model
    openai_temperature: float = 0.0  # deterministic for analytics
    llm_base_url: str = "https://api.groq.com/openai/v1"

    # Per-attempt LLM timeout. Was a hardcoded 15.0 in IntentExtractor, chosen so a
    # struggling primary reached the fallback chain quickly. That reasoning inverted
    # once the chain stopped working: Groq retired `llama-3.1-8b-instant` (404) and
    # OpenRouter is out of credit (402), so failing fast now buys nothing and just
    # converts a slow success into a 400.
    #
    # Measured against the real 9,912-token prompt: 2.81 / 3.58 / 3.97 / 4.55 / 5.00
    # / 5.16 s, median 4.55 s. So 15 s was ~3x the median and production STILL hit it
    # twice (15.16 s and 15.28 s -- both exactly at the ceiling, meaning the request
    # was still in flight when we gave up, not that Google had gone silent). Google
    # also returns intermittent 503 "experiencing high demand" on this key.
    #
    # A slow answer beats no answer, and when Gemini is healthy this costs nothing --
    # it returns in ~5 s either way.
    # 40.0 -> 25.0 on 2026-09-08. 40 was chosen when the failure mode looked like
    # "Gemini is sometimes slower than 15 s"; production then showed it is sometimes
    # UNREACHABLE from that host, returning nothing at all. Two 40 s black holes
    # plus the dead rungs produced a 81,154 ms 400 for a query that answers in
    # 1.1-7.5 s locally on the identical 10,213-token prompt.
    #
    # With no working fallback the worst case is timeout + ~1 s, so this number IS
    # the user's wait on a bad day. 25 s still covers every latency actually
    # observed (max 17.2 s, p50 ~4.5 s) while bounding the failure at ~26 s.
    # Retrying no longer compounds it: see _is_transient_llm_error, which excludes
    # timeouts precisely because they are transient but expensive.
    llm_timeout_seconds: float = 25.0

    # Retries for the PRIMARY only, on transient errors (timeout / 429 / 503).
    # SDK-level retries stay off (max_retries=0): those retry every error including
    # deterministic 4xx, which is what made a rate-limited primary block for minutes.
    # This is narrower -- a transient class, on the one rung that works.
    llm_primary_retries: int = 1

    # Tertiary: Google Gemini (via OpenAI compat)
    google_api_key: str = ""
    google_model: str = "gemini-3.1-flash-lite"
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

    # Order the provider chain is walked, first entry is the primary. Comma
    # separated; unknown names are ignored and a provider with no API key is
    # skipped, so a partial list degrades rather than breaking.
    #
    # The default keeps today's behaviour. Set it to
    # "openrouter,google,groq" on a host where Google's endpoint is unreachable —
    # which is the situation on Render, where the direct Gemini call times out at
    # whatever ceiling is configured (15.16 s under a 15 s limit, 40.36 s under 40 s:
    # always exactly at the ceiling, i.e. no response rather than a slow one) while
    # the same instance reaches OpenRouter in ~400 ms.
    #
    # Deliberately config and not code: CLAUDE.md's rule is that swapping or
    # reordering providers touches config.py and the client construction, never the
    # call sites.
    llm_provider_order: str = "google,groq,openrouter"

    def provider_chain(self) -> list[tuple[str, str, str, str]]:
        """Ordered [(label, api_key, base_url, model)], skipping unconfigured ones.

        One source of truth for BOTH consumers — IntentExtractor's three rungs and
        `_chat_with_fallback()` for the narrative and schema answers. They each had
        their own hardcoded google->groq->openrouter list, so a reordering would
        have silently applied to one path and not the other.

        Note the field names are historically misleading and left alone:
        `openai_api_key` / `openai_model` are the GROQ credentials.
        """
        # getattr with defaults throughout, so this also works when called UNBOUND
        # against a duck-typed stand-in: `Settings.provider_chain(fake_settings)`.
        # Test doubles are SimpleNamespace or MagicMock and rarely carry every
        # field, and the alternative was a second copy of this logic living in
        # intent_extractor.py as a fallback — which is exactly the duplication this
        # method exists to remove.
        get = lambda name, default="": getattr(self, name, default)  # noqa: E731
        available = {
            "google": (get("google_api_key"), get("google_base_url"),
                       get("google_model")),
            "groq": (get("openai_api_key"), get("llm_base_url"),
                     get("openai_model")),
            "openrouter": (get("openrouter_api_key"), get("openrouter_base_url"),
                           get("openrouter_model")),
        }
        order = get("llm_provider_order", "google,groq,openrouter") or ""
        chain: list[tuple[str, str, str, str]] = []
        for label in (part.strip().lower() for part in order.split(",")):
            spec = available.get(label)
            if spec is None or not spec[0]:
                continue
            chain.append((label, spec[0], spec[1], spec[2]))
        return chain

    # ----------------------------------------------------------------- Warehouse
    # Which engine serves queries. Switched to duckdb on 2026-08-03: the Snowflake
    # trial ended and all of its virtual warehouses were suspended, so every query
    # returned 503. Set warehouse_engine=snowflake to switch back once billing is
    # restored — the Snowflake settings below are kept intact for exactly that.
    #
    # This flag must agree with the dbt target (DBT_TARGET=duckdb|dev), because the
    # in-process MetricFlow engine compiles through dbt's profiles.yml. main.py
    # exports DUCKDB_PATH for dbt/MetricFlow so both sides resolve one file.
    warehouse_engine: str = "duckdb"

    # Path to the DuckDB file, relative to the gateway working directory.
    # The FILENAME matters: DuckDB derives the catalog name from it, which is what
    # makes `database: streaming_analytics` in the dbt sources and the dashboard
    # route's STREAMING_ANALYTICS.marts.<table> names resolve unchanged.
    # Build it with load_raw_data_to_duckdb.py + `dbt run`.
    duckdb_path: str = "../streaming_analytics.duckdb"

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
    # Root of the dbt project — MetricFlow needs dbt_project.yml and profiles.yml here.
    dbt_project_dir: str = "../dbt_streaming_analytics/streaming_analytics"
    # Hold one warm in-process MetricFlowEngine instead of spawning `mf` per compile.
    # Measured on this project: 0.02s per compile vs 22-34s for the subprocess (~500x),
    # and SQL verified byte-identical to the subprocess across every shape tested.
    #
    # Built LAZILY by SQLGenerator on the first template-cache miss, so startup stays
    # fast and a process that never misses pays neither the ~15s build nor the ~100 MB
    # resident cost. Both success and failure are memoised.
    #
    # Set false to force the `mf` subprocess — it remains the fallback on any engine
    # failure, and the pre-compiled templates in .sql_template_cache.json still cover
    # the common combos, so turning this off degrades latency but never correctness.
    # Worth watching resident memory on a 512 MB instance: measured 150 -> 251 MB.
    metricflow_in_process: bool = True

    # ── Diagnostics: the "why" path (core/diagnostics/) ──────────────────────
    # Off by default. `langgraph` costs 10.7s of import and +68.6MB RSS (measured),
    # and this instance already sits near 251MB of 512MB with the warm MetricFlow
    # engine loaded — so when this is false the import never happens at all. The
    # graph is built lazily on the first diagnostic query and memoised, never in
    # lifespan(), for the same reason.
    diagnostics_enabled: bool = False
    # Decomposition axes per diagnosis. Each costs 2 probes, or 4 for a metric with
    # a weight_metric. Three is 14 probes for a weighted metric, ~60ms each warm.
    diagnostics_max_dimensions: int = 3
    # Raised from 16 when the reflect loop landed. 16 fitted ONE round: churn_rate
    # plans 15 probes (3 baseline/comparison/trend + 4 per weighted axis x 3 axes),
    # so a second round had one probe of headroom and could not afford the pair any
    # axis needs. The loop would have fired, planned nothing, and looked like it
    # simply found no cause.
    #
    # 40 is the measured worst case across the driver graph, not a guess:
    # engagement_rate has 9 usable axes, all reachable within 3 rounds x 3
    # dimensions, and weighted, giving 3 + 9*4 = 39. Every other metric is lower.
    #
    # This is a runaway guard, not a performance one. At ~60 ms per warm probe, 39
    # probes is ~2.4 s of warehouse time against a 25 s deadline — so
    # `diagnostics_deadline_seconds` is what actually bounds a slow run, and this
    # bounds a planner bug.
    diagnostics_max_probes: int = 40
    # A diagnosis is bounded by wall clock as well as probe count, because the
    # characteristic failure of this kind of agent is not stopping.
    diagnostics_deadline_seconds: float = 25.0
    # Months of history a diagnosis looks at when the question names no period.
    # The current month is excluded: fct_mrr_monthly's spine runs to current_date()
    # while cancellations carry a +1 month offset, so the newest month is
    # structurally churn-only and reads as a collapse.
    diagnostics_default_months: int = 6

    # Build the engine on a background thread at startup instead of on the first
    # cache miss. Without this the ~27s build lands inside whichever request
    # misses first — production 2026-07-31 logged a 30.3s query that was 27.1s
    # build + 0.2s compile. Startup already waits ~21s on the Snowflake pool, so
    # the build overlaps it and is normally ready before the first user.
    #
    # Defaults to False under pytest: tests/test_query_endpoint.py boots the real
    # lifespan per test, so ~17 concurrent engine builds would burn CPU and ~100 MB
    # each. Tests never miss the template cache, so they never need the engine.
    metricflow_prewarm: bool = "pytest" not in sys.modules

    # Where the compiled-template artifact lives. Overridable so the test suite does
    # not write to the COMMITTED build artifact: test_query_endpoint.py boots the real
    # lifespan per test, which loads (and, via CacheWarmer, wrote to) this file and
    # left a spurious git diff after every run. Under pytest it points at a temp file.
    sql_template_cache_path: str = (
        os.path.join(tempfile.gettempdir(), "sql_template_cache.test.json")
        if "pytest" in sys.modules
        else "./.sql_template_cache.json"
    )
    semantic_models_path: str = (
        "../dbt_streaming_analytics/streaming_analytics/models/semantic"
    )

    # ----------------------------------------------------------------- Runtime
    # Skip the ChromaDB retrieval path and inject the full user-facing metric list
    # into the prompt instead. True in production and effectively the only mode
    # that runs: `chroma_store/` is gitignored so the index is never deployed, and
    # even locally MetricEmbedder raises because the persisted collection was built
    # with a different embedding function. With only ~20 metrics the full list fits
    # comfortably, and RAG is the wrong lever for latency now that the warehouse is
    # local — see CLAUDE.md.
    #
    # The field name IS the contract: pydantic-settings has no env_prefix here and
    # `case_sensitive=False`, so `disable_rag` reads the existing DISABLE_RAG
    # environment variable. Do not rename it or invert its polarity — a field named
    # e.g. `rag_enabled` would silently ignore DISABLE_RAG=true and turn retrieval
    # back on in production. Read here rather than via os.getenv so the value is
    # typed, defaulted and visible in one place, per the repo's config convention.
    disable_rag: bool = False

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

    # ------------------------------------------------- Memory (RAM) observability
    # The caps above were tuned against measured numbers (warm engine 150 -> 251 MB)
    # that nothing actually reported at runtime, so an OOM restart on Render read
    # like any other restart. These flags render RSS into the logs via core/memory.py.
    #
    #   log_memory=false                 kills every RAM log line at once
    #   log_memory_per_request=false     keeps startup + heartbeat, drops the per-request suffix
    #   memory_log_interval_seconds=0    disables the idle heartbeat
    #
    # Sampling uses psutil when installed and falls back to /proc/self/statm
    # (Linux/Render) or GetProcessMemoryInfo (Windows), so it degrades to "n/a"
    # rather than failing.
    log_memory: bool = True

    # Append `rss=…` to each request log line. Cheap (one integer read next to a
    # ~1.5 s Snowflake round trip) and it is what ties a spike to a specific query.
    log_memory_per_request: bool = True

    # Background heartbeat interval. The per-request line only fires while traffic
    # flows, so it cannot show drift on an idle instance — which is exactly the
    # shape of a leak that ends in an OOM restart. 0 disables.
    #
    # Off under pytest: test_query_endpoint.py boots the real lifespan per test, and
    # ~17 daemon threads logging RAM would add noise without ever running long
    # enough to sample twice.
    memory_log_interval_seconds: int = 0 if "pytest" in sys.modules else 300

    # Memory ceiling used for the "% of limit" figure. 0 = auto-detect from the
    # cgroup, which is the correct number on Render (the container limit is what
    # triggers the OOM kill, not the host's total RAM). Set explicitly only when
    # running somewhere the cgroup is not readable.
    memory_limit_mb: int = 0

    # Above this percentage of the limit, RAM status lines log at WARNING.
    memory_warn_pct: float = 85.0


    warmup_matrix: dict[str, list[str]] = {
        # MetricFlow-validated dimension names only.
        # Prefixes must match the entity defined in the semantic model:
        #   subscription__ → fct_mrr_monthly / subscription entity
        #   subscriber__   → dim_subscribers / subscriber entity
        #   session__      → fct_stream_sessions / session entity
        #   event__        → stg_recommendation_events / event entity
        #   payment__      → fct_payments / payment entity
        #
        # NOTE: In production the runtime CacheWarmer is disabled (DISABLE_CACHE_WARMER=true);
        # this matrix is consumed OFFLINE by precompile_templates.py, whose output
        # (.sql_template_cache.json) ships as a committed build artifact. So matrix size
        # only affects local precompile time, not Render RAM — the old "≤6 combos on
        # 512 MB" warning no longer applies.
        #
        # DIMENSION DISCIPLINE (see referential-integrity analysis):
        #   - fct_mrr_monthly / dim_subscribers / fct_payments joins are sound → these
        #     metrics may be sliced by subscription__ / subscriber__ / payment__ dims.
        #   - fct_stream_sessions → subscriber/content joins break for the latest-month
        #     append, so SESSION metrics are warmed by NATIVE session dims ONLY
        #     (session__device_type/quality_streamed/referral_source). Never warm a
        #     session metric by subscriber__* — it returns a null-dominated breakdown
        #     for the current month.
        #
        # ── MRR family (fct_mrr_monthly — subscriber join 100%) ──
        "mrr":                   ["subscription__plan_type", "subscriber__country", "subscriber__cohort_month"],
        "expansion_mrr":         ["subscription__plan_type", "subscriber__country"],
        "total_revenue":         ["subscriber__plan_type", "subscriber__country"],
        # net_mrr_growth is intentionally NOT warmed: it's a derived offset_window
        # (month-over-month) metric, so MetricFlow can't compile it without a
        # metric_time grouping and it always falls back. The dashboard net_mrr_growth_kpi
        # widget computes it via its own SQL builder; NL queries use the governed fallback.
        # churn_rate/retention_rate: monthly event-based, on fct_mrr_monthly.
        "churn_rate":            ["subscription__plan_type", "subscriber__country", "subscriber__churn_reason"],
        "retention_rate":        ["subscription__plan_type", "subscriber__country"],
        # ── Subscriber counts (dim_subscribers — base table, no join risk) ──
        "total_subscribers":     ["subscriber__plan_type", "subscriber__country", "subscriber__acquisition_channel"],
        "churned_subscribers":   ["subscriber__plan_type", "subscriber__country"],
        # ── LTV (fct_payments — subscriber join ~86%) ──
        "ltv":                   ["subscriber__plan_type", "subscriber__country"],
        # ── Session metrics (fct_stream_sessions — NATIVE session dims only) ──
        "avg_watch_time":        ["session__device_type", "session__quality_streamed", "session__referral_source"],
        "total_sessions":        ["session__device_type", "session__quality_streamed", "session__referral_source"],
        "engagement_rate":       ["session__device_type", "session__quality_streamed"],
        # ── Recommendation metrics (stg_recommendation_events — native) ──
        "recommendation_ctr":    ["event__recommendation_type"],
        "total_recommendations": ["event__recommendation_type"],
        "clicked_recommendations": ["event__recommendation_type"],
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

    # API key for cost-bearing routes (/query, /dashboard). When non-empty,
    # every request to those routes must include an ``X-API-Key`` header (or
    # ``api_key`` query param) that matches this value.  Leave empty for
    # unauthenticated development access.
    gateway_api_key: str = ""

    # Comma-separated list of allowed CORS origins. Defaults to ``*`` for
    # development; restrict to your frontend URL in production, e.g.
    # ``https://streaming-analytics.onrender.com``.
    cors_allowed_origins: str = "*"


# Module-level singleton — importable everywhere without re-parsing .env
settings = Settings()
