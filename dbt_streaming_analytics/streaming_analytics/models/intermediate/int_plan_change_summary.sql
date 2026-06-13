-- Model: int_plan_change_summary
-- Layer: intermediate
-- Grain: One row per subscriber
-- Dependencies: stg_subscription_plan_history, stg_subscribers

with plan_history as (
    select * from {{ ref('stg_subscription_plan_history') }}
),

subscribers as (
    select subscriber_id, plan_type as current_plan
    from {{ ref('stg_subscribers') }}
),

aggregated as (
    select
        subscriber_id,
        count(case when change_type = 'upgrade' then 1 end) as total_upgrades,
        count(case when change_type = 'downgrade' then 1 end) as total_downgrades,
        max(change_date) as last_change_date,
        sum(new_mrr_usd - old_mrr_usd) as net_mrr_change_usd
    from plan_history
    group by 1
),

last_change as (
    select distinct
        subscriber_id,
        first_value(change_type) over (partition by subscriber_id order by change_date desc) as last_change_type
    from plan_history
),

final as (
    select
        s.subscriber_id,
        coalesce(a.total_upgrades, 0) as total_upgrades,
        coalesce(a.total_downgrades, 0) as total_downgrades,
        a.last_change_date,
        lc.last_change_type,
        s.current_plan,
        coalesce(a.net_mrr_change_usd, 0) as net_mrr_change_usd
    from subscribers s
    left join aggregated a on s.subscriber_id = a.subscriber_id
    left join last_change lc on s.subscriber_id = lc.subscriber_id
)

select * from final
