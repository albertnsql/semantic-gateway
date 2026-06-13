# Streaming Analytics dbt Project

## Project Overview
This project contains the complete data transformation layer for a Netflix-style streaming platform, built with dbt and MetricFlow. The warehouse is designed to run on Snowflake, integrating robust data models from raw ingestion all the way through to certified, AI-ready semantic metrics.

It transforms 10 raw operational tables (subscribers, payments, stream sessions, etc.) into a star schema composed of dimension and fact tables. Finally, it layers MetricFlow semantic definitions on top to power consistent aggregations across dashboards, ad-hoc queries, and AI assistants.

## Architecture Diagram

```
[ Raw Layer (Snowflake) ]
       |
       v
[ Staging (Views) ] --------> Cleans names, casts types, adds audit cols
       |
       v
[ Intermediate (Views) ] ---> Business logic: session aggregation, cohorts, period spines
       |
       v
[ Marts (Tables) ] ---------> Star schema: Dimensions & Facts (subscribers, sessions, MRR)
       |
       v
[ Semantic Layer (MetricFlow) ] -> Certified metrics, entities, and dimensions
```

## Layer Descriptions
- **Staging (`staging/`)**: Materialized as views. This layer does simple renaming, type casting, and adds an `_loaded_at` audit column. No complex business logic.
- **Intermediate (`intermediate/`)**: Materialized as views. This layer holds the complex business logic such as generating period spines, aggregating sessions per subscriber/content, and determining monthly cohorts.
- **Marts (`marts/`)**: Materialized as tables. This is the final presentation layer structured as a dimensional star schema containing Dimension tables (e.g., `dim_subscribers`, `dim_dates`) and Fact tables (e.g., `fct_stream_sessions`, `fct_mrr_monthly`).
- **Semantic (`semantic/` & `metrics/`)**: Defined via YAML. Contains MetricFlow definitions to expose certified metrics directly to downstream tools.

## Certified Metrics

| Metric Name | Grain | Description | Source Model |
|-------------|-------|-------------|--------------|
| **Monthly Recurring Revenue (MRR)** | subscription_id + period_month | Total MRR across active subscriptions. | `fct_mrr_monthly` |
| **Subscriber Churn Rate** | churn_date | Ratio of churned subscribers to total subscribers. | `dim_subscribers` |
| **Content Engagement Rate** | session_id | Average watch completion across all sessions. | `fct_stream_sessions` |
| **Lifetime Value (LTV)** | subscriber_id | Total revenue per subscriber lifetime. | `fct_payments` |
| **Expansion MRR** | subscription_id + period_month | MRR gained from plan upgrades. | `fct_mrr_monthly` |
| **Recommendation CTR** | event_timestamp | Recommendation click-through rate. | `stg_recommendation_events` |

## How to Run

To run this project locally, ensure you have configured your `profiles.yml` for Snowflake.

```bash
# Install dependencies (if any)
dbt deps

# Build the entire project (run models + tests)
dbt build

# Run just the tests
dbt test

# Compile the project and verify syntax
dbt compile
```

## Grain Safety Warning
**IMPORTANT:** Be extremely careful when joining different fact tables (e.g., MRR data with session-level data). Direct AI-to-SQL joins across differing grains will cause dangerous fanouts, leading to duplicated revenue or distorted engagement metrics. 

Always query through the **MetricFlow Semantic Layer** instead of writing raw SQL joins. MetricFlow automatically handles query generation and safely aggregates facts before joining to shared dimensions, ensuring correct mathematical results.
