import re
import os

with open('gateway/api/routes/dashboard.py', 'r') as f:
    content = f.read()

# 1. Update _resolve_yoy_dates
content = content.replace(
    'def _resolve_yoy_dates(years: list[int]) -> dict:',
    'def _resolve_yoy_dates(years: list[int], max_data_date: str) -> dict:'
)

content = content.replace(
    'if current_year >= _DEFAULT_CURRENT_YEAR:\n        data_through_date = _DEFAULT_DATA_THROUGH          # 2026-06-01',
    'if current_year >= int(max_data_date[:4]):\n        data_through_date = max_data_date'
)

# 2. Update widget SQL functions to accept max_data_date
for func in ['_sql_revenue_kpi', '_sql_mrr_kpi', '_sql_subs_kpi', '_sql_watch_time_kpi', '_sql_net_mrr_growth_kpi', '_sql_engagement_kpi', '_sql_churn_rate_kpi', '_sql_sub_dist', '_sql_mrr_bridge', '_sql_mrr_trend', '_sql_retention_trend', '_sql_sessions_trend', '_sql_watch_time_content_type']:
    content = content.replace(
        f'def {func}(plans: list[str], years: list[int], countries: list[str]) -> str:',
        f'def {func}(plans: list[str], years: list[int], countries: list[str], max_data_date: str) -> str:'
    )
    content = content.replace(
        f'd = _resolve_yoy_dates(years)',
        f'd = _resolve_yoy_dates(years, max_data_date)'
    )

# Fix _sql_mrr_kpi delegation
content = content.replace(
    'return _sql_revenue_kpi(plans, years, countries)',
    'return _sql_revenue_kpi(plans, years, countries, max_data_date)'
)

# For widgets without d = _resolve_yoy_dates, replace '2026-06-01' with max_data_date
content = re.sub(
    r"AND period_month >= DATEADD\(month, -12, '2026-06-01'::date\)\s*AND period_month <= '2026-06-01'::date",
    "AND period_month >= DATEADD(month, -12, '{max_data_date}'::date)\\n  AND period_month <= '{max_data_date}'::date",
    content
)
content = re.sub(
    r"AND period_month >= DATEADD\('month', -12, '2026-06-01'::date\)\s*AND period_month <= '2026-06-01'::date",
    "AND period_month >= DATEADD('month', -12, '{max_data_date}'::date)\\n  AND period_month <= '{max_data_date}'::date",
    content
)
content = re.sub(
    r"WHERE period_month >= DATEADD\('month', -12, '2026-06-01'::date\)\s*AND period_month <= '2026-06-01'::date",
    "WHERE period_month >= DATEADD('month', -12, '{max_data_date}'::date)\\n  AND period_month <= '{max_data_date}'::date",
    content
)

content = content.replace(
    "DATEADD('month', -12, '2026-06-01'::date)",
    "DATEADD('month', -12, '{max_data_date}'::date)"
)
content = content.replace(
    "AND session_start < '2026-07-01'::date",
    "AND session_start < DATEADD('month', 1, '{max_data_date}'::date)"
)

# 3. Widget type signature
content = content.replace(
    '_WIDGET_SQL: dict[str, Callable[[list[str], list[int], list[str]], str]] = {',
    '_WIDGET_SQL: dict[str, Callable[[list[str], list[int], list[str], str], str]] = {'
)

# 4. In get_dashboard_widget, fetch max_date and pass it
fetch_max_date_code = """
    # ── 3.5 Fetch max date ────────────────────────────────────────────────────
    query_cache = getattr(request.app.state, "query_cache", None)
    max_date = _DEFAULT_DATA_THROUGH
    if query_cache is not None:
        cached_md = query_cache.get({"type": "max_date"})
        if cached_md:
            max_date = cached_md["date"]
        else:
            try:
                rows = request.app.state.sql_generator.execute_query(
                    f"SELECT MAX(period_month) as max_date FROM {_DB}.marts.fct_mrr_monthly WHERE is_active = TRUE"
                )
                if rows and rows[0].get("MAX_DATE"):
                    max_date = str(rows[0]["MAX_DATE"])[:10]
                elif rows and rows[0].get("max_date"):
                    max_date = str(rows[0]["max_date"])[:10]
                query_cache.set({"type": "max_date"}, {"date": max_date})
            except Exception as e:
                logger.error(f"Failed to fetch max_date: {e}")

    # ── 4. Build pre-certified SQL ────────────────────────────────────────────
    sql_fn = _WIDGET_SQL[widget]
    compiled_sql = sql_fn(parsed_plans, parsed_years, parsed_countries, max_date)
"""
content = re.sub(
    r'# ── 4\. Build pre-certified SQL ────────────────────────────────────────────.*?compiled_sql = sql_fn\(parsed_plans, parsed_years, parsed_countries\)',
    fetch_max_date_code.strip(),
    content,
    flags=re.DOTALL
)

# 5. Add /metadata endpoint
metadata_route = """
@router.get(
    "/dashboard/metadata",
    summary="Fetch dashboard metadata",
    tags=["Dashboard"],
)
async def get_dashboard_metadata(request: Request) -> JSONResponse:
    query_cache = getattr(request.app.state, "query_cache", None)
    max_date = _DEFAULT_DATA_THROUGH
    if query_cache is not None:
        cached_md = query_cache.get({"type": "max_date"})
        if cached_md:
            max_date = cached_md["date"]
        else:
            try:
                rows = request.app.state.sql_generator.execute_query(
                    f"SELECT MAX(period_month) as max_date FROM {_DB}.marts.fct_mrr_monthly WHERE is_active = TRUE"
                )
                if rows and rows[0].get("MAX_DATE"):
                    max_date = str(rows[0]["MAX_DATE"])[:10]
                elif rows and rows[0].get("max_date"):
                    max_date = str(rows[0]["max_date"])[:10]
                query_cache.set({"type": "max_date"}, {"date": max_date})
            except Exception as e:
                pass
    return JSONResponse(content={"max_date": max_date})

@router.get(
    "/dashboard/widgets",
"""
content = content.replace('@router.get(\n    "/dashboard/widgets",', metadata_route.strip() + '\n')

with open('gateway/api/routes/dashboard.py', 'w') as f:
    f.write(content)
print('Done modifying dashboard.py')
