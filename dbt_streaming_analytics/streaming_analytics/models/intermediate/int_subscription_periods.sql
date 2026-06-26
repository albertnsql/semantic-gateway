-- Model: int_subscription_periods
-- Layer: intermediate
-- Grain: One row per subscription per month
-- Dependencies: stg_subscriptions

with subscriptions as (
    select * from {{ ref('stg_subscriptions') }}
),

-- generate a date spine (we can use generator or a simple macro, but since it's an intermediate model we'll use a standard CTE recursive approach or dbt_utils if available. 
-- Since we are strictly using Snowflake and avoiding dbt_utils if we don't have it installed, we can generate months using a generator.)
months as (
    select dateadd(month, seq4(), '2020-01-01'::date) as period_month
    from table(generator(rowcount => 120)) -- 10 years of months
),

subscription_periods as (
    select
        s.subscription_id,
        s.subscriber_id,
        s.plan_type,
        s.mrr_usd,
        m.period_month,
        s.billing_cycle,
        s.status,
        case 
            when s.status = 'active' then true
            when m.period_month <= date_trunc('month', coalesce(s.end_date, current_date())) then true
            else false 
        end as is_active
    from subscriptions s
    join months m
        on m.period_month >= date_trunc('month', s.start_date)
        and m.period_month <= case 
            when s.status = 'active' then dateadd(month, -1, date_trunc('month', current_date()))
            else dateadd(month, 1, date_trunc('month', coalesce(s.end_date, current_date())))
        end
)

select * from subscription_periods
