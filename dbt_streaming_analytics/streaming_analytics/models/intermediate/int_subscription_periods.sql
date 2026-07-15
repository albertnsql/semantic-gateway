-- Model: int_subscription_periods
-- Layer: intermediate
-- Grain: One row per subscription per month
-- Dependencies: stg_subscriptions, stg_subscription_plan_history

with subscriptions as (
    select * from {{ ref('stg_subscriptions') }}
),

plan_history as (
    select * from {{ ref('stg_subscription_plan_history') }}
),

months as (
    select dateadd(month, seq4(), '2020-01-01'::date) as period_month
    from table(generator(rowcount => 120)) -- 10 years of months
),

-- 1. Get the very first plan for subscribers who changed plans
first_changes as (
    select 
        subscriber_id,
        old_plan as initial_plan,
        old_mrr_usd as initial_mrr
    from (
        select subscriber_id, old_plan, old_mrr_usd,
               row_number() over (partition by subscriber_id order by change_date asc) as rn
        from plan_history
    )
    where rn = 1
),

-- 2. Build the base timeline (from signup to first change, or to infinity if no changes)
base_timeline as (
    select
        s.subscription_id,
        s.subscriber_id,
        coalesce(fc.initial_plan, s.plan_type) as plan_type,
        coalesce(fc.initial_mrr, s.mrr_usd) as mrr_usd,
        s.start_date as valid_from,
        s.billing_cycle,
        s.status,
        s.end_date
    from subscriptions s
    left join first_changes fc on s.subscriber_id = fc.subscriber_id
),

-- 3. Add all plan change events as new timeline segments
change_events as (
    select
        s.subscription_id,
        ph.subscriber_id,
        ph.new_plan as plan_type,
        ph.new_mrr_usd as mrr_usd,
        ph.change_date as valid_from,
        s.billing_cycle,
        s.status,
        s.end_date
    from plan_history ph
    join subscriptions s on ph.subscriber_id = s.subscriber_id
),

-- 4. Union and build SCD Type 2 timeline
scd_timeline as (
    select
        subscription_id,
        subscriber_id,
        plan_type,
        mrr_usd,
        billing_cycle,
        status,
        end_date,
        valid_from,
        coalesce(
            lead(valid_from) over (partition by subscription_id order by valid_from),
            current_date() + interval '100 years'
        ) as valid_to
    from (
        select * from base_timeline
        union all
        select * from change_events
    )
),

-- 5. Join to the month spine
subscription_periods as (
    select
        t.subscription_id,
        t.subscriber_id,
        t.plan_type,
        t.mrr_usd,
        m.period_month,
        t.billing_cycle,
        t.status,
        case 
            when t.status = 'active' then true
            when m.period_month <= date_trunc('month', coalesce(t.end_date, current_date())) then true
            else false 
        end as is_active
    from scd_timeline t
    join months m
        on m.period_month >= date_trunc('month', t.valid_from)
        and m.period_month < date_trunc('month', t.valid_to)
        and m.period_month <= case 
            when t.status = 'active' then date_trunc('month', current_date())
            else dateadd(month, 1, date_trunc('month', coalesce(t.end_date, current_date())))
        end
)

select * from subscription_periods
