"""
api/routes/dashboard.py — GET /api/v1/dashboard/{widget}

Dedicated high-performance endpoint for dashboard widget data.

Bypasses the full NL pipeline (Classify → RAG → Intent Extract → Validate)
and executes pre-certified SQL templates directly against the Snowflake pool.

This is safe because dashboard widgets are fixed, known queries authored by
the data team — they do not accept free-form natural language input. Filter
parameters are whitelisted before any injection into SQL.

Performance profile (vs /api/v1/query):
  - Zero LLM calls (saves ~1-2s per widget)
  - Zero RAG embedding (saves ~200ms per widget)
  - Parallel-friendly: all 11 widgets can fire concurrently from the frontend
  - Cached: same QueryCache as /query (TTL 3600s)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
import decimal

def make_json_safe(obj):
    if isinstance(obj, list):
        return [make_json_safe(i) for i in obj]
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return obj

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Dashboard"])

# ── Constants ─────────────────────────────────────────────────────────────────

_DB = "STREAMING_ANALYTICS"

# Whitelisted plan values — must match actual PLAN_TYPE values in fct_mrr_monthly / dim_subscribers
_ALLOWED_PLANS: frozenset[str] = frozenset({"basic", "standard", "premium"})

# Whitelisted year values — expand as needed
_ALLOWED_YEARS: frozenset[int] = frozenset({2021, 2022, 2023, 2024, 2025, 2026})

_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset({"movie", "series", "documentary", "short"})
_ALLOWED_COUNTRIES: frozenset[str] = frozenset({"US", "IN", "GB", "DE", "BR"})


# ── Filter clause builders (safe, whitelisted) ────────────────────────────────

def _plan_clause(plans: list[str], col: str = "plan_type") -> str:
    """Return a SQL AND clause for plan_type filtering, or empty string."""
    if not plans:
        return ""
    quoted = ", ".join(f"'{p}'" for p in plans)
    return f"AND {col} IN ({quoted})"


def _year_clause(years: list[int], col: str) -> str:
    """Return a SQL AND clause for year filtering via YEAR(), or empty string."""
    if not years:
        return ""
    return f"AND YEAR({col}) IN ({', '.join(str(y) for y in years)})"

def _country_clause_sub(countries: list[str]) -> str:
    if not countries:
        return ""
    quoted = ", ".join(f"'{c}'" for c in countries)
    return f"AND subscriber_id IN (SELECT subscriber_id FROM {_DB}.marts.dim_subscribers WHERE country IN ({quoted}))"

def _country_clause(countries: list[str], col: str = "country") -> str:
    if not countries:
        return ""
    quoted = ", ".join(f"'{c}'" for c in countries)
    return f"AND {col} IN ({quoted})"


# ── Pre-certified SQL builders ────────────────────────────────────────────────
# Each function accepts (plans, years, content_types, countries) and returns a ready-to-execute SQL string.
# SQL is static — only safe whitelisted filter values are interpolated.

def _sql_mrr_kpi(plans: list[str], years: list[int], countries: list[str]) -> str:
    year_filter = _year_clause(years, 'period_month')
    return f"""
-- Dashboard: mrr_kpi — Total MRR
SELECT 
    period_month,
    COALESCE(SUM(mrr_usd), 0) AS value
FROM {_DB}.marts.fct_mrr_monthly
WHERE 1=1 {year_filter}
  AND period_month <= '2026-05-01'::date
  AND is_active = TRUE
  {_plan_clause(plans)}
  {_country_clause_sub(countries)}
GROUP BY 1
ORDER BY 1 DESC
LIMIT 2
""".strip()


def _sql_subs_kpi(plans: list[str], years: list[int], countries: list[str]) -> str:
    year_filter = _year_clause(years, 'period_month')
    return f"""
-- Dashboard: subs_kpi — Active (non-churned) subscriber count
SELECT 
    period_month,
    COUNT(DISTINCT subscriber_id) AS value
FROM {_DB}.marts.fct_mrr_monthly
WHERE 1=1 {year_filter}
  AND period_month <= '2026-05-01'::date
  AND is_active = TRUE
  {_plan_clause(plans)}
  {_country_clause_sub(countries)}
GROUP BY 1
ORDER BY 1 DESC
LIMIT 2
""".strip()


def _sql_watch_time_kpi(plans: list[str], years: list[int], countries: list[str]) -> str:
    year_clause = _year_clause(years, "session_start") if years else ""
    plan_filter = f"AND subscriber_id IN (SELECT subscriber_id FROM {_DB}.marts.dim_subscribers WHERE 1=1 {_plan_clause(plans)})" if plans else ""
    return f"""
-- Dashboard: watch_time_kpi — Average watch time
SELECT 
    DATE_TRUNC('month', session_start) AS period_month,
    AVG(duration_minutes) AS value
FROM {_DB}.marts.fct_stream_sessions
WHERE 1=1 {year_clause}
  AND session_start < '2026-06-01'::date
  {plan_filter}
  {_country_clause(countries)}
GROUP BY 1
ORDER BY 1 DESC
LIMIT 2
""".strip()


def _sql_net_mrr_growth_kpi(plans: list[str], years: list[int], countries: list[str]) -> str:
    year_filter = _year_clause(years, 'period_month')
    plan_filter = _plan_clause(plans)
    country_filter = _country_clause_sub(countries)
    return f"""
-- Dashboard: net_mrr_growth_kpi — Net MRR Growth % vs prior month
-- mrr_usd is always stored as a positive value in fct_mrr_monthly;
-- contraction and churned rows are negated here to compute net change.
WITH monthly AS (
    SELECT
        period_month,
        SUM(CASE WHEN mrr_type IN ('new','expansion') THEN mrr_usd
                 WHEN mrr_type IN ('contraction','churned') THEN -mrr_usd
                 ELSE 0 END) AS net_change,
        SUM(CASE WHEN is_active = TRUE THEN mrr_usd ELSE 0 END) AS total_mrr
    FROM {_DB}.marts.fct_mrr_monthly
    WHERE 1=1 {year_filter}
      AND period_month <= '2026-05-01'::date
      {plan_filter}
      {country_filter}
    GROUP BY 1
)
SELECT
    period_month,
    net_change / NULLIF(LAG(total_mrr) OVER (ORDER BY period_month), 0) AS value
FROM monthly
ORDER BY period_month DESC
LIMIT 2
""".strip()


def _sql_engagement_kpi(plans: list[str], years: list[int], countries: list[str]) -> str:
    year_clause = _year_clause(years, "session_start") if years else ""
    plan_filter = f"AND subscriber_id IN (SELECT subscriber_id FROM {_DB}.marts.dim_subscribers WHERE 1=1 {_plan_clause(plans)})" if plans else ""
    return f"""
-- Dashboard: engagement_kpi — Average content completion rate
SELECT 
    DATE_TRUNC('month', session_start) AS period_month,
    AVG(completion_pct) * 100.0 AS value
FROM {_DB}.marts.fct_stream_sessions
WHERE 1=1 {year_clause}
  AND session_start < '2026-06-01'::date
  {plan_filter}
  {_country_clause_sub(countries)}
GROUP BY 1
ORDER BY 1 DESC
LIMIT 2
""".strip()


def _sql_churn_rate_kpi(plans: list[str], years: list[int], countries: list[str]) -> str:
    # Churn Rate = churned subscriptions / total active-or-churned subscriptions per month.
    # Sourced directly from fct_mrr_monthly — same pattern as all other KPI tiles.
    # Mirrors the certified MetricFlow metric (churned_subscribers / total_subscribers)
    # without going through the MetricFlow query path (no MetricFlow runner in this route).
    year_filter = _year_clause(years, 'period_month')
    plan_filter = _plan_clause(plans)
    country_filter = _country_clause_sub(countries)
    return f"""
-- Dashboard: churn_rate_kpi — Monthly churn rate (churned / total subscribers)
SELECT
    period_month,
    COUNT(CASE WHEN mrr_type = 'churned' THEN 1 END)::FLOAT /
    NULLIF(COUNT(DISTINCT CASE WHEN mrr_type != 'inactive' THEN subscriber_id END), 0) AS value
FROM {_DB}.marts.fct_mrr_monthly
WHERE 1=1 {year_filter}
  AND period_month <= '2026-05-01'::date
  {plan_filter}
  {country_filter}
GROUP BY 1
ORDER BY 1 DESC
LIMIT 2
""".strip()


def _sql_sub_dist(plans: list[str], years: list[int], countries: list[str]) -> str:
    year_filter = _year_clause(years, 'period_month')
    return f"""
-- Dashboard: sub_dist — Subscriber count by plan type (pie chart)
SELECT 
    INITCAP(plan_type) AS name, 
    COUNT(DISTINCT subscriber_id) AS value
FROM {_DB}.marts.fct_mrr_monthly
WHERE period_month = (
    SELECT MAX(period_month)
    FROM {_DB}.marts.fct_mrr_monthly
    WHERE 1=1 {year_filter}
      AND period_month <= '2026-05-01'::date
)
  AND is_active = TRUE
  {_plan_clause(plans)}
  {_country_clause_sub(countries)}
GROUP BY 1
ORDER BY 2 DESC
""".strip()


def _sql_mrr_bridge(plans: list[str], years: list[int], countries: list[str]) -> str:
    # Returns one row per (period_month, mrr_type) for the last 12 months.
    # mrr_usd is always positive in fct_mrr_monthly; contraction and churned rows
    # are negated here so the frontend can stack them below the zero baseline.
    plan_filter = _plan_clause(plans)
    country_filter = _country_clause_sub(countries)
    return f"""
-- Dashboard: mrr_bridge — MRR bridge by component (12 months, unpivoted)
SELECT
    period_month,
    mrr_type,
    SUM(CASE WHEN mrr_type IN ('contraction','churned') THEN -mrr_usd ELSE mrr_usd END) AS value
FROM {_DB}.marts.fct_mrr_monthly
WHERE mrr_type IN ('new', 'expansion', 'contraction', 'churned')
  AND period_month >= DATEADD(month, -12, '2026-05-01'::date)
  AND period_month <= '2026-05-01'::date
  {plan_filter}
  {country_filter}
GROUP BY 1, 2
ORDER BY 1
""".strip()


def _sql_mrr_trend(plans: list[str], years: list[int], countries: list[str]) -> str:
    return f"""
-- Dashboard: mrr_trend — MRR by month for the selected period (area chart)
SELECT
    period_month                AS name,
    SUM(mrr_usd)                AS mrr
FROM {_DB}.marts.fct_mrr_monthly
WHERE is_active = TRUE
  AND period_month >= DATEADD('month', -12, '2026-05-01'::date)
  AND period_month <= '2026-05-01'::date
  {_plan_clause(plans)}
  {_country_clause_sub(countries)}
GROUP BY period_month
ORDER BY period_month
""".strip()


def _sql_retention_trend(plans: list[str], years: list[int], countries: list[str]) -> str:
    return f"""
-- Dashboard: retention_trend — Retention Rate Trend (12 months)
SELECT
    TO_CHAR(DATE_TRUNC('month', period_month), 'Mon YYYY') AS name,
    DATE_TRUNC('month', period_month) AS sort_key,
    ROUND(
      COUNT(CASE WHEN mrr_type = 'retained' THEN 1 END) * 100.0 /
      NULLIF(COUNT(DISTINCT subscription_id), 0), 1
    ) AS retention_rate
FROM {_DB}.marts.fct_mrr_monthly
WHERE period_month >= DATEADD('month', -12, '2026-05-01'::date)
  AND period_month <= '2026-05-01'::date
  {_plan_clause(plans)}
  {_country_clause_sub(countries)}
GROUP BY DATE_TRUNC('month', period_month), name
ORDER BY sort_key ASC
""".strip()


def _sql_sessions_trend(plans: list[str], years: list[int], countries: list[str]) -> str:
    plan_filter = f"AND subscriber_id IN (SELECT subscriber_id FROM {_DB}.marts.dim_subscribers WHERE 1=1 {_plan_clause(plans)})" if plans else ""
    return f"""
-- Dashboard: sessions_trend — Stream session count by month, last 12 months (bar chart)
SELECT
    TO_CHAR(DATE_TRUNC('month', session_start), 'Mon YYYY') AS name,
    DATE_TRUNC('month', session_start)                       AS sort_key,
    COUNT(*)                                                     AS sessions
FROM {_DB}.marts.fct_stream_sessions
WHERE session_start >= DATEADD('month', -12, '2026-05-01'::date)
  AND session_start < '2026-06-01'::date
  {plan_filter}
  {_country_clause(countries)}
GROUP BY DATE_TRUNC('month', session_start), name
ORDER BY sort_key
""".strip()

def _sql_watch_time_content_type(plans: list[str], years: list[int], countries: list[str]) -> str:
    return f"""
-- Dashboard: watch_time_content_type — Avg Watch Time by Content Type
SELECT c.content_type AS name, ROUND(AVG(s.duration_minutes), 1) AS value
FROM {_DB}.marts.fct_stream_sessions s
JOIN {_DB}.marts.dim_content c
  ON s.content_id = c.content_id
GROUP BY c.content_type 
ORDER BY value DESC
""".strip()


# ── Widget registry — name → SQL builder function ──────────────────────────────

_WIDGET_SQL: dict[str, Callable[[list[str], list[int], list[str]], str]] = {
    "mrr_kpi":              _sql_mrr_kpi,
    "subs_kpi":             _sql_subs_kpi,
    "watch_time_kpi":       _sql_watch_time_kpi,
    "net_mrr_growth_kpi":   _sql_net_mrr_growth_kpi,
    "engagement_kpi":       _sql_engagement_kpi,
    "churn_rate_kpi":       _sql_churn_rate_kpi,
    "sub_dist":             _sql_sub_dist,
    "mrr_bridge":           _sql_mrr_bridge,
    "mrr_trend":            _sql_mrr_trend,
    "retention_trend":      _sql_retention_trend,
    "sessions_trend":       _sql_sessions_trend,
    "watch_time_content_type": _sql_watch_time_content_type,
}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get(
    "/dashboard/widgets",
    summary="List all available dashboard widget IDs",
    description="Returns the set of widget IDs accepted by GET /api/v1/dashboard/{widget}.",
    tags=["Dashboard"],
)
async def list_widgets() -> JSONResponse:
    """Return all registered widget IDs and their filter support."""
    return JSONResponse(
        status_code=200,
        content={
            "widgets": sorted(_WIDGET_SQL.keys()),
            "filter_options": {
                "plan_types": sorted(_ALLOWED_PLANS),
                "years": sorted(_ALLOWED_YEARS),
            },
        },
    )


@router.get(
    "/dashboard/{widget}",
    summary="Fetch a dashboard widget's data directly from Snowflake",
    description=(
        "Returns pre-certified, LLM-free query results for a named dashboard widget.\n\n"
        "Supports optional `plan_types` and `years` query-string filters.\n\n"
        "Results are served from the gateway cache (TTL 3600s) on repeated calls.\n\n"
        "**~10× faster** than `/api/v1/query` for dashboard use-cases — zero LLM calls."
    ),
    tags=["Dashboard"],
)
async def get_dashboard_widget(
    widget: str,
    request: Request,
    plan_types: str = Query(
        default="",
        description="Comma-separated plan type values, e.g. 'Enterprise,Pro'. "
                    "Omit or leave empty to include all plans.",
    ),
    years: str = Query(
        default="",
        description="Comma-separated years, e.g. '2023,2024'. Omit to include all.",
    ),
    countries: str = Query(
        default="",
        description="Comma-separated country codes, e.g. 'US,GB'. Omit to include all.",
    ),
) -> JSONResponse:
    """
    Execute a pre-certified SQL template for the requested dashboard widget.

    Pipeline:
      1. Validate widget name
      2. Whitelist-validate filter parameters
      3. Cache lookup (skip Snowflake if hit)
      4. Build pre-certified SQL with safe filter injection
      5. Execute against the shared Snowflake pool
      6. Cache and return

    No LLM calls, no RAG, no semantic validation overhead.
    """
    request_id = str(uuid.uuid4())

    # ── 1. Validate widget ────────────────────────────────────────────────────
    if widget not in _WIDGET_SQL:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_widget",
                "message": f"Widget '{widget}' does not exist.",
                "valid_widgets": sorted(_WIDGET_SQL.keys()),
            },
        )

    # ── 2. Whitelist-validate filters ─────────────────────────────────────────
    parsed_plans: list[str] = []
    if plan_types.strip():
        for p in plan_types.split(","):
            p = p.strip()
            if p in _ALLOWED_PLANS:
                parsed_plans.append(p)
            elif p:
                logger.warning(
                    "[%s] Skipping unknown plan_type filter value: '%s'", request_id, p
                )

    parsed_years: list[int] = []
    if years.strip():
        for y in years.split(","):
            y = y.strip()
            try:
                yi = int(y)
                if yi in _ALLOWED_YEARS:
                    parsed_years.append(yi)
                else:
                    logger.warning(
                        "[%s] Skipping out-of-range year filter: %d", request_id, yi
                    )
            except ValueError:
                logger.warning(
                    "[%s] Skipping non-integer year filter value: '%s'", request_id, y
                )

    parsed_countries: list[str] = []
    if countries.strip():
        for c in countries.split(","):
            c = c.strip()
            if c in _ALLOWED_COUNTRIES:
                parsed_countries.append(c)

    # ── 3. Cache lookup ───────────────────────────────────────────────────────
    query_cache = getattr(request.app.state, "query_cache", None)
    cache_key = {
        "widget": widget,
        "plans": sorted(parsed_plans),
        "years": sorted(parsed_years),
        "countries": sorted(parsed_countries),
    }

    if query_cache is not None:
        cached = query_cache.get(cache_key)
        if cached is not None:
            logger.info(
                "[%s] Dashboard CACHE HIT widget=%s plans=%s years=%s",
                request_id, widget, parsed_plans, parsed_years,
            )
            cached = make_json_safe(cached)
            return JSONResponse(
                status_code=200,
                content={**cached, "cache_hit": True, "request_id": request_id},
                headers={"X-Cache": "HIT"},
            )

    # ── 4. Build pre-certified SQL ────────────────────────────────────────────
    sql_fn = _WIDGET_SQL[widget]
    compiled_sql = sql_fn(parsed_plans, parsed_years, parsed_countries)
    logger.info(
        "[%s] Dashboard widget=%s plans=%s years=%s countries=%s — executing SQL",
        request_id, widget, parsed_plans or "all", parsed_years or "all", parsed_countries or "all"
    )

    # ── 5. Execute against Snowflake pool ────────────────────────────────────
    sql_gen = request.app.state.sql_generator
    try:
        rows: list[dict] = sql_gen.execute_query(compiled_sql)
    except Exception as exc:
        logger.error(
            "[%s] Dashboard Snowflake error widget=%s: %s", request_id, widget, exc
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "snowflake_unavailable",
                "widget": widget,
                "message": str(exc),
            },
        )

    payload = {
        "widget": widget,
        "data": jsonable_encoder(rows),
        "row_count": len(rows),
        "cache_hit": False,
    }

    # ── 6. Store in cache ─────────────────────────────────────────────────────
    payload = make_json_safe(payload)
    if query_cache is not None:
        query_cache.set(cache_key, payload)

    logger.info(
        "[%s] Dashboard widget=%s returned %d rows", request_id, widget, len(rows)
    )
    return JSONResponse(
        status_code=200,
        content={**payload, "request_id": request_id},
        headers={"X-Cache": "MISS"},
    )
