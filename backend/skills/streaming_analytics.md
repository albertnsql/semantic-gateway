---
name: streaming-analytics-skill
version: 1.0.0
description: "IF the user asks about MRR, subscribers, churn, watch time,
  retention, LTV, engagement, content performance, or plan revenue —
  THEN this skill applies. DO NOT use for infrastructure, pipeline
  debugging, or schema questions."
---

## Section 1 — Semantic Layer

ALWAYS attempt the semantic layer first before falling back to raw SQL.

### Available MetricFlow Metrics

- mrr
- new_mrr
- churned_mrr
- active_subscribers
- avg_watch_time
- engagement_rate
- ltv

### Available Dimensions

- plan_type
- country
- content_type
- month

Only fall back to raw SQL if the requested metric is not in this list or if MetricFlow returns an error.

## Section 2 — Table Reference

### fct_mrr_monthly
- **Grain:** One row per subscriber per month
- **Key dimensions:** `plan_type`, `country`

### fct_stream_sessions
- **Grain:** One session per row
- **Key dimensions:** `content_type`, `country`
- **Important:** Use `AVG(watch_time_minutes)` — never SUM
- Standard hygiene filter: `WHERE is_deleted = FALSE`

### dim_content
- **Grain:** One row per `content_id`
- **Join key:** `content_id` — used to resolve `content_type` from `fct_stream_sessions`
- Standard hygiene filter: `WHERE is_deleted = FALSE`

**Standard hygiene filter:** Exclude `is_deleted = TRUE` on ALL tables in every query, without exception.

## Section 3 — Gotchas

- **MRR fan-out:** Never join `fct_mrr_monthly` to `fct_stream_sessions` directly — they are at different grains and will produce row multiplication.
- **MRR Aggregation:** MRR (Monthly Recurring Revenue) is a monthly snapshot metric. NEVER aggregate or sum it across multiple months. If a user asks for MRR over a multi-month period (e.g. "for the year 2026"), you MUST set `aggregation_level` to `"month"` so the semantic layer returns the trend, rather than summing it into a meaningless annual total.
- **Watch time:** Always use `AVG(watch_time_minutes)` not `SUM(watch_time_minutes)` — summing sessions gives meaningless totals.
- **Churn:** `churned_mrr` is already a signed negative value; do not negate it again or the sign will flip to positive.
- **Country NULL:** Some rows have `country = NULL` (unknown origin); always include `WHERE country IS NOT NULL` unless the user explicitly asks to include all countries.
- **Plan type:** Valid values are `'basic'`, `'standard'`, `'premium'` only — any other value indicates bad data and should be filtered out.

## Section 4 — Analysis Patterns

### MRR Trend
Use `fct_mrr_monthly` grouped by `month` and `plan_type`.

```sql
SELECT
    DATE_TRUNC('month', period_month) AS month,
    plan_type,
    SUM(mrr_usd) AS mrr
FROM fct_mrr_monthly
WHERE country IS NOT NULL
  AND plan_type IN ('basic', 'standard', 'premium')
GROUP BY 1, 2
ORDER BY 1, 2
```

### Retention (Cohort)
Cohort on `first_subscription_date`, then track whether the subscriber has `is_active = TRUE` in subsequent months.

```sql
SELECT
    DATE_TRUNC('month', first_subscription_date) AS cohort_month,
    DATE_TRUNC('month', period_month)             AS activity_month,
    COUNT(DISTINCT subscriber_id)                 AS active_subscribers
FROM fct_mrr_monthly
GROUP BY 1, 2
ORDER BY 1, 2
```

### Watch Time by Content
Join `fct_stream_sessions` to `dim_content` on `content_id`, then group by `content_type`.

```sql
SELECT
    dc.content_type,
    AVG(fs.watch_time_minutes) AS avg_watch_time
FROM fct_stream_sessions fs
JOIN dim_content dc
    ON fs.content_id = dc.content_id
WHERE fs.is_deleted = FALSE
  AND dc.is_deleted = FALSE
  AND fs.country IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC
```

## Section 5 — Provenance Footer

Every answer must end with the following footer (fill in the bracketed values at runtime):

```
Source: [semantic layer | governed mart | raw table]
Freshness: [MAX(date) from queried table]
Reviewed: [sql_reviewer ✓ | skipped — no SQL generated]
```
