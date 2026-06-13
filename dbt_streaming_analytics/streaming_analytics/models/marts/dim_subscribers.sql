-- Model: dim_subscribers
-- Layer: marts
-- Grain: One row per subscriber
-- Dependencies: stg_subscribers, int_subscriber_engagement, int_monthly_cohorts, int_plan_change_summary

with subscribers as (
    select * from {{ ref('stg_subscribers') }}
),

engagement as (
    -- Get lifetime totals and averages across all months
    select
        subscriber_id,
        sum(total_watch_minutes) as lifetime_watch_minutes,
        avg(total_sessions) as avg_monthly_sessions
    from {{ ref('int_subscriber_engagement') }}
    group by 1
),

cohorts as (
    select * from {{ ref('int_monthly_cohorts') }}
),

plan_changes as (
    select * from {{ ref('int_plan_change_summary') }}
),

final as (
    select
        s.subscriber_id,
        s.email,
        s.country,
        s.signup_date,
        c.cohort_month,
        s.acquisition_channel,
        s.plan_type,
        s.plan_price_usd,
        s.subscription_status,
        s.churn_date,
        s.churn_reason,
        s.age_group,
        s.device_preference,
        coalesce(e.lifetime_watch_minutes, 0) as lifetime_watch_minutes,
        coalesce(e.avg_monthly_sessions, 0) as avg_monthly_sessions,
        coalesce(p.total_upgrades, 0) as total_upgrades,
        coalesce(p.total_downgrades, 0) as total_downgrades,
        case when s.subscription_status = 'churned' then true else false end as is_churned,
        datediff('day', s.signup_date, coalesce(s.churn_date, current_date())) as tenure_days
    from subscribers s
    left join engagement e on s.subscriber_id = e.subscriber_id
    left join cohorts c on s.subscriber_id = c.subscriber_id
    left join plan_changes p on s.subscriber_id = p.subscriber_id
)

select * from final
