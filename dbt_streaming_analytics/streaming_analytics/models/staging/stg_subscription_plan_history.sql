-- Model: stg_subscription_plan_history
-- Layer: staging
-- Grain: One row per plan change event
-- Dependencies: raw.subscription_plan_history

with source as (
    select * from {{ source('raw', 'subscription_plan_history') }}
),

final as (
    select
        cast(change_id as varchar) as change_id,
        cast(subscriber_id as varchar) as subscriber_id,
        cast(old_plan as varchar) as old_plan,
        cast(new_plan as varchar) as new_plan,
        cast(old_mrr_usd as decimal(10,2)) as old_mrr_usd,
        cast(new_mrr_usd as decimal(10,2)) as new_mrr_usd,
        cast(change_type as varchar) as change_type,
        cast(change_date as date) as change_date,
        cast(change_reason as varchar) as change_reason,
        current_timestamp() as _loaded_at
    from source
)

select * from final
