-- Model: int_monthly_cohorts
-- Layer: intermediate
-- Grain: One row per subscriber cohort assignment
-- Dependencies: stg_subscribers

with subscribers as (
    select * from {{ ref('stg_subscribers') }}
),

cohorts as (
    select
        subscriber_id,
        date_trunc('month', signup_date) as cohort_month,
        acquisition_channel,
        plan_type,
        country
    from subscribers
)

select * from cohorts
