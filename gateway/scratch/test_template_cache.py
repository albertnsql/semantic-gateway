"""
test_template_cache.py — Offline SQL template cache validation.

Simulates the full lifecycle for all 22 warmup combinations:
  1. Store a template (post-reviewer SQL — with WHERE hygiene clause, no date filter)
  2. Call inject_time_filter with a real date range
  3. Validate the result:
     - Has no syntax like "GROUP BY ... WHERE" or "GROUP BY ... AND"
     - Has exactly one WHERE clause
     - Date filter appears before GROUP BY
     - No double WHERE keywords

Run from: gateway directory
    python scratch/test_template_cache.py
"""

import sys
import os
import re

# ── make sure gateway modules are importable ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.sql_template_cache import (
    SQLTemplateCache,
    strip_time_filter,
    inject_time_filter,
)

# ──────────────────────────────────────────────── realistic reviewed SQL samples

# These mimic what the SQL reviewer produces for warmup queries (no date range)
# Each has: WHERE hygiene clause, then GROUP BY — the critical ordering.

SQL_SAMPLES = {

    # mrr × plan_type  (fct_mrr_monthly, period_month)
    ("mrr", "subscription__plan_type"): """\
SELECT
    subq_2.plan_type AS subscription__plan_type,
    SUM(subq_1.mrr_usd) AS mrr
FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly AS subq_1
WHERE
    subq_1.is_active = TRUE
    AND subq_1.plan_type IN ('basic', 'standard', 'premium')
GROUP BY
    subq_2.plan_type
""",

    # mrr × country  (join to dim_subscribers)
    ("mrr", "subscriber__country"): """\
SELECT
    subq_3.country AS subscriber__country,
    SUM(subq_1.mrr_usd) AS mrr
FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly AS subq_1
LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers AS subq_3
    ON subq_1.subscriber_id = subq_3.subscriber_id
WHERE
    subq_1.is_active = TRUE
    AND subq_3.country IS NOT NULL
GROUP BY
    subq_3.country
""",

    # mrr × cohort_month
    ("mrr", "subscriber__cohort_month"): """\
SELECT
    subq_3.cohort_month AS subscriber__cohort_month,
    SUM(subq_1.mrr_usd) AS mrr
FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly AS subq_1
LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers AS subq_3
    ON subq_1.subscriber_id = subq_3.subscriber_id
WHERE
    subq_1.is_active = TRUE
GROUP BY
    subq_3.cohort_month
""",

    # total_subscribers × plan_type
    ("total_subscribers", "subscriber__plan_type"): """\
SELECT
    subq_1.plan_type AS subscriber__plan_type,
    COUNT(subq_1.subscriber_id) AS total_subscribers
FROM STREAMING_ANALYTICS.marts.dim_subscribers AS subq_1
WHERE
    subq_1.plan_type IN ('basic', 'standard', 'premium')
GROUP BY
    subq_1.plan_type
""",

    # total_subscribers × country
    ("total_subscribers", "subscriber__country"): """\
SELECT
    subq_1.country AS subscriber__country,
    COUNT(subq_1.subscriber_id) AS total_subscribers
FROM STREAMING_ANALYTICS.marts.dim_subscribers AS subq_1
WHERE
    subq_1.country IS NOT NULL
GROUP BY
    subq_1.country
""",

    # total_subscribers × acquisition_channel
    ("total_subscribers", "subscriber__acquisition_channel"): """\
SELECT
    subq_1.acquisition_channel AS subscriber__acquisition_channel,
    COUNT(subq_1.subscriber_id) AS total_subscribers
FROM STREAMING_ANALYTICS.marts.dim_subscribers AS subq_1
WHERE
    subq_1.acquisition_channel IS NOT NULL
GROUP BY
    subq_1.acquisition_channel
""",

    # churn_rate × plan_type
    ("churn_rate", "subscriber__plan_type"): """\
SELECT
    subq_1.plan_type AS subscriber__plan_type,
    NULLIF(SUM(CASE WHEN subq_1.is_churned = TRUE THEN 1 ELSE 0 END), 0)
        / NULLIF(COUNT(subq_1.subscriber_id), 0) AS churn_rate
FROM STREAMING_ANALYTICS.marts.dim_subscribers AS subq_1
WHERE
    subq_1.plan_type IN ('basic', 'standard', 'premium')
GROUP BY
    subq_1.plan_type
""",

    # churn_rate × country
    ("churn_rate", "subscriber__country"): """\
SELECT
    subq_1.country AS subscriber__country,
    NULLIF(SUM(CASE WHEN subq_1.is_churned = TRUE THEN 1 ELSE 0 END), 0)
        / NULLIF(COUNT(subq_1.subscriber_id), 0) AS churn_rate
FROM STREAMING_ANALYTICS.marts.dim_subscribers AS subq_1
WHERE
    subq_1.country IS NOT NULL
GROUP BY
    subq_1.country
""",

    # churn_rate × churn_reason
    ("churn_rate", "subscriber__churn_reason"): """\
SELECT
    subq_1.churn_reason AS subscriber__churn_reason,
    NULLIF(SUM(CASE WHEN subq_1.is_churned = TRUE THEN 1 ELSE 0 END), 0)
        / NULLIF(COUNT(subq_1.subscriber_id), 0) AS churn_rate
FROM STREAMING_ANALYTICS.marts.dim_subscribers AS subq_1
WHERE
    subq_1.is_churned = TRUE
GROUP BY
    subq_1.churn_reason
""",

    # churned_subscribers × plan_type
    ("churned_subscribers", "subscriber__plan_type"): """\
SELECT
    subq_1.plan_type AS subscriber__plan_type,
    COUNT(CASE WHEN subq_1.is_churned = TRUE THEN subq_1.subscriber_id END)
        AS churned_subscribers
FROM STREAMING_ANALYTICS.marts.dim_subscribers AS subq_1
WHERE
    subq_1.plan_type IN ('basic', 'standard', 'premium')
GROUP BY
    subq_1.plan_type
""",

    # churned_subscribers × country
    ("churned_subscribers", "subscriber__country"): """\
SELECT
    subq_1.country AS subscriber__country,
    COUNT(CASE WHEN subq_1.is_churned = TRUE THEN subq_1.subscriber_id END)
        AS churned_subscribers
FROM STREAMING_ANALYTICS.marts.dim_subscribers AS subq_1
WHERE
    subq_1.country IS NOT NULL
GROUP BY
    subq_1.country
""",

    # ltv × payment_method
    ("ltv", "payment__payment_method"): """\
SELECT
    subq_1.payment_method AS payment__payment_method,
    SUM(subq_1.amount_usd) AS ltv
FROM STREAMING_ANALYTICS.marts.fct_payments AS subq_1
WHERE
    subq_1.status = 'succeeded'
GROUP BY
    subq_1.payment_method
""",

    # ltv × plan_type
    ("ltv", "subscriber__plan_type"): """\
SELECT
    subq_2.plan_type AS subscriber__plan_type,
    SUM(subq_1.amount_usd) AS ltv
FROM STREAMING_ANALYTICS.marts.fct_payments AS subq_1
LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers AS subq_2
    ON subq_1.subscriber_id = subq_2.subscriber_id
WHERE
    subq_1.status = 'succeeded'
    AND subq_2.plan_type IN ('basic', 'standard', 'premium')
GROUP BY
    subq_2.plan_type
""",

    # ltv × country
    ("ltv", "subscriber__country"): """\
SELECT
    subq_2.country AS subscriber__country,
    SUM(subq_1.amount_usd) AS ltv
FROM STREAMING_ANALYTICS.marts.fct_payments AS subq_1
LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers AS subq_2
    ON subq_1.subscriber_id = subq_2.subscriber_id
WHERE
    subq_1.status = 'succeeded'
    AND subq_2.country IS NOT NULL
GROUP BY
    subq_2.country
""",

    # expansion_mrr × plan_type
    ("expansion_mrr", "subscription__plan_type"): """\
SELECT
    subq_1.plan_type AS subscription__plan_type,
    SUM(subq_1.mrr_usd) AS expansion_mrr
FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly AS subq_1
WHERE
    subq_1.is_active = TRUE
    AND subq_1.mrr_type = 'expansion'
    AND subq_1.plan_type IN ('basic', 'standard', 'premium')
GROUP BY
    subq_1.plan_type
""",

    # expansion_mrr × country
    ("expansion_mrr", "subscriber__country"): """\
SELECT
    subq_2.country AS subscriber__country,
    SUM(subq_1.mrr_usd) AS expansion_mrr
FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly AS subq_1
LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers AS subq_2
    ON subq_1.subscriber_id = subq_2.subscriber_id
WHERE
    subq_1.is_active = TRUE
    AND subq_1.mrr_type = 'expansion'
    AND subq_2.country IS NOT NULL
GROUP BY
    subq_2.country
""",

    # engagement_rate × device_type
    ("engagement_rate", "session__device_type"): """\
SELECT
    subq_1.device_type AS session__device_type,
    AVG(subq_1.engagement_minutes) AS engagement_rate
FROM STREAMING_ANALYTICS.marts.fct_stream_sessions AS subq_1
WHERE
    subq_1.device_type IS NOT NULL
GROUP BY
    subq_1.device_type
""",

    # engagement_rate × plan_type  (join to dim_subscribers)
    ("engagement_rate", "subscriber__plan_type"): """\
SELECT
    subq_2.plan_type AS subscriber__plan_type,
    AVG(subq_1.engagement_minutes) AS engagement_rate
FROM STREAMING_ANALYTICS.marts.fct_stream_sessions AS subq_1
LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers AS subq_2
    ON subq_1.subscriber_id = subq_2.subscriber_id
WHERE
    subq_2.plan_type IN ('basic', 'standard', 'premium')
GROUP BY
    subq_2.plan_type
""",

    # engagement_rate × country
    ("engagement_rate", "subscriber__country"): """\
SELECT
    subq_2.country AS subscriber__country,
    AVG(subq_1.engagement_minutes) AS engagement_rate
FROM STREAMING_ANALYTICS.marts.fct_stream_sessions AS subq_1
LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers AS subq_2
    ON subq_1.subscriber_id = subq_2.subscriber_id
WHERE
    subq_2.country IS NOT NULL
GROUP BY
    subq_2.country
""",

    # recommendation_ctr × recommendation_type
    ("recommendation_ctr", "event__recommendation_type"): """\
SELECT
    subq_1.recommendation_type AS event__recommendation_type,
    NULLIF(SUM(CASE WHEN subq_1.was_clicked = TRUE THEN 1 ELSE 0 END), 0)
        / NULLIF(COUNT(subq_1.event_id), 0) AS recommendation_ctr
FROM STREAMING_ANALYTICS.staging.stg_recommendation_events AS subq_1
WHERE
    subq_1.recommendation_type IS NOT NULL
GROUP BY
    subq_1.recommendation_type
""",

    # total_recommendations × recommendation_type
    ("total_recommendations", "event__recommendation_type"): """\
SELECT
    subq_1.recommendation_type AS event__recommendation_type,
    COUNT(subq_1.event_id) AS total_recommendations
FROM STREAMING_ANALYTICS.staging.stg_recommendation_events AS subq_1
WHERE
    subq_1.recommendation_type IS NOT NULL
GROUP BY
    subq_1.recommendation_type
""",

    # clicked_recommendations × recommendation_type
    ("clicked_recommendations", "event__recommendation_type"): """\
SELECT
    subq_1.recommendation_type AS event__recommendation_type,
    COUNT(CASE WHEN subq_1.was_clicked = TRUE THEN subq_1.event_id END)
        AS clicked_recommendations
FROM STREAMING_ANALYTICS.staging.stg_recommendation_events AS subq_1
WHERE
    subq_1.recommendation_type IS NOT NULL
GROUP BY
    subq_1.recommendation_type
""",
}

# Which physical time column each metric uses (mirrors _METRIC_TIME_COL in sql_generator.py)
METRIC_TIME_COL = {
    "mrr":                    "period_month",
    "expansion_mrr":          "period_month",
    "churn_rate":             "signup_date",
    "churned_subscribers":    "signup_date",
    "total_subscribers":      "signup_date",
    "recommendation_ctr":     "event_timestamp",
    "total_recommendations":  "event_timestamp",
    "clicked_recommendations":"event_timestamp",
    "ltv":                    "payment_date",
    "engagement_rate":        "session_start",
}

START_DATE = "2026-03-12"
END_DATE   = "2026-06-11"

# ──────────────────────────────────────────────── validation helpers

def count_keyword(sql: str, kw: str) -> int:
    return len(re.findall(rf"\b{kw}\b", sql, re.IGNORECASE))

def find_keyword_line(sql: str, kw: str):
    """Return 1-based line numbers of all occurrences of kw."""
    lines = []
    for i, line in enumerate(sql.splitlines(), 1):
        if re.search(rf"\b{kw}\b", line, re.IGNORECASE):
            lines.append(i)
    return lines

def where_before_groupby(sql: str) -> bool:
    """True if the WHERE keyword appears on an earlier line than GROUP BY."""
    where_lines = find_keyword_line(sql, "WHERE")
    groupby_lines = find_keyword_line(sql, "GROUP BY")
    if not where_lines or not groupby_lines:
        return True  # no conflict possible
    return max(where_lines) < min(groupby_lines)

def validate_injected_sql(combo_label: str, sql: str, time_col: str) -> list[str]:
    """Return list of error strings (empty = pass)."""
    errors = []

    # 1. Must have exactly one WHERE
    n_where = count_keyword(sql, "WHERE")
    if n_where == 0:
        errors.append("MISSING WHERE clause — date filter not injected")
    elif n_where > 1:
        errors.append(f"DOUBLE WHERE clause ({n_where} occurrences)")

    # 2. WHERE must appear before GROUP BY
    if not where_before_groupby(sql):
        errors.append("WHERE appears AFTER GROUP BY — syntax error in Snowflake")

    # 3. Date literals must be present
    if START_DATE not in sql:
        errors.append(f"Start date {START_DATE!r} not found in SQL")
    if END_DATE not in sql:
        errors.append(f"End date {END_DATE!r} not found in SQL")

    # 4. time_col must appear in a WHERE/AND clause (not just SELECT/GROUP BY)
    where_block_match = re.search(r"\bWHERE\b(.*?)(?:\bGROUP\b|\bORDER\b|\bHAVING\b|\bLIMIT\b|$)",
                                  sql, re.IGNORECASE | re.DOTALL)
    if where_block_match:
        where_block = where_block_match.group(1)
        if time_col not in where_block:
            errors.append(f"time_col '{time_col}' not found inside WHERE block")
    else:
        errors.append("Could not locate WHERE block for time_col check")

    # 5. No placeholder tokens should survive injection
    if "__TEMPLATE_START_DATE__" in sql or "__TEMPLATE_END_DATE__" in sql:
        errors.append("Unreplaced placeholder tokens remain in SQL")

    return errors


# ──────────────────────────────────────────────── main test runner

def run_tests():
    cache = SQLTemplateCache(ttl_seconds=86400, maxsize=200)

    passed = 0
    failed = 0
    results = []

    print("=" * 70)
    print("SQL Template Cache — Injection Verification")
    print(f"Test date range: {START_DATE} to {END_DATE}")
    print("=" * 70)

    for (metric, dim), reviewed_sql in SQL_SAMPLES.items():
        label = f"{metric} × {dim}"

        # ── Step 1: strip any existing time filter (warmup SQL has none here)
        sql_template, detected_col = strip_time_filter(reviewed_sql)
        time_col = METRIC_TIME_COL.get(metric) or detected_col

        # ── Step 2: store in cache
        has_placeholder = "__TEMPLATE_START_DATE__" in sql_template
        cache.set([metric], [dim], sql_template, time_col, has_placeholder)

        # ── Step 3: retrieve from cache
        entry = cache.get([metric], [dim])
        assert entry is not None, f"Cache miss immediately after SET for {label}"

        # ── Step 4: inject time filter
        injected_sql = inject_time_filter(
            entry["sql_template"],
            entry["time_col"] or time_col,
            START_DATE,
            END_DATE,
        )

        # ── Step 5: validate
        errors = validate_injected_sql(label, injected_sql, time_col)

        if errors:
            failed += 1
            status = "FAIL ✗"
        else:
            passed += 1
            status = "PASS ✓"

        results.append((label, status, errors, injected_sql))

    # ──────────────── also test the PLACEHOLDER path ─────────────────────────
    # Simulate a query that DID have a date range: MetricFlow compiled with dates,
    # strip replaces them with placeholders, inject re-inserts real dates.
    PLACEHOLDER_CASES = [
        {
            "label": "[placeholder] mrr × plan_type (MF included date filter)",
            "metric": "mrr",
            "dim": "subscription__plan_type",
            "sql": (
                "SELECT plan_type, SUM(mrr_usd) AS mrr\n"
                "FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly\n"
                "WHERE is_active = TRUE\n"
                "  AND plan_type IN ('basic', 'standard', 'premium')\n"
                "  AND period_month >= '2025-01-01' AND period_month <= '2025-12-31'\n"
                "GROUP BY plan_type\n"
            ),
        },
        {
            "label": "[placeholder] ltv × payment_method (BETWEEN syntax)",
            "metric": "ltv",
            "dim": "payment__payment_method",
            "sql": (
                "SELECT payment_method, SUM(amount_usd) AS ltv\n"
                "FROM STREAMING_ANALYTICS.marts.fct_payments\n"
                "WHERE status = 'succeeded'\n"
                "  AND payment_date BETWEEN '2025-01-01' AND '2025-12-31'\n"
                "GROUP BY payment_method\n"
            ),
        },
    ]

    for case in PLACEHOLDER_CASES:
        label = case["label"]
        metric = case["metric"]
        dim = case["dim"]
        time_col = METRIC_TIME_COL[metric]

        sql_template, detected_col = strip_time_filter(case["sql"])
        has_placeholder = "__TEMPLATE_START_DATE__" in sql_template

        injected_sql = inject_time_filter(sql_template, time_col, START_DATE, END_DATE)
        errors = validate_injected_sql(label, injected_sql, time_col)

        if errors:
            failed += 1
            status = "FAIL ✗"
        else:
            passed += 1
            status = "PASS ✓"

        results.append((label, status, errors, injected_sql))

    # ──────────────── print results ───────────────────────────────────────────
    print()
    for label, status, errors, injected_sql in results:
        print(f"  {status}  {label}")
        if errors:
            for e in errors:
                print(f"          ERROR: {e}")

    print()
    print("=" * 70)
    print(f"Results: {passed} passed, {failed} failed  ({passed + failed} total)")
    print("=" * 70)

    if failed > 0:
        print("\n── FAILED SQL OUTPUTS ───────────────────────────────────────────────")
        for label, status, errors, injected_sql in results:
            if status.startswith("FAIL"):
                print(f"\n  Combo: {label}")
                print("  Injected SQL:")
                for i, line in enumerate(injected_sql.splitlines(), 1):
                    print(f"    {i:3}: {line}")
        sys.exit(1)
    else:
        print("\nAll combinations produce syntactically valid SQL. ✓")
        sys.exit(0)


if __name__ == "__main__":
    # Suppress cache SET/GET log noise for clean test output
    import logging
    logging.basicConfig(level=logging.WARNING)
    run_tests()
