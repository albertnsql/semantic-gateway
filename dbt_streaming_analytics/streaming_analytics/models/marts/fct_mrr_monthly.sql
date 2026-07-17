-- Model: fct_mrr_monthly
-- Layer: marts
-- Grain: One row per subscription per month
-- Dependencies: int_subscription_periods, int_plan_change_summary

with subscription_periods as (
    select * from {{ ref('int_subscription_periods') }}
),

plan_changes as (
    select * from {{ ref('int_plan_change_summary') }}
),

mrr_with_lag as (
    select
        sp.subscription_id,
        sp.subscriber_id,
        sp.period_month,
        sp.plan_type,
        sp.mrr_usd,
        sp.billing_cycle,
        sp.is_active,
        lag(sp.mrr_usd) over (partition by sp.subscription_id order by sp.period_month) as prev_mrr_usd,
        lag(sp.is_active) over (partition by sp.subscription_id order by sp.period_month) as prev_is_active
    from subscription_periods sp
),

final as (
    select
        subscription_id,
        subscriber_id,
        period_month,
        plan_type,
        mrr_usd,
        -- Month-over-month MRR movement for this subscription:
        -- new = full amount, expansion = upgrade delta, contraction = negative delta.
        mrr_usd - coalesce(prev_mrr_usd, 0) as mrr_change_usd,
        billing_cycle,
        is_active,
        case
            when prev_mrr_usd is null and is_active then 'new'
            when mrr_usd > coalesce(prev_mrr_usd, 0) and is_active then 'expansion'
            when mrr_usd < prev_mrr_usd and is_active then 'contraction'
            when is_active = false and prev_is_active = true then 'churned'
            when is_active = false then 'inactive'
            else 'retained'
        end as mrr_type
    from mrr_with_lag
)

select * from final
