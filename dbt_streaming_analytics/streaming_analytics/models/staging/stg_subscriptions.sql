-- Model: stg_subscriptions
-- Layer: staging
-- Grain: One row per subscription
-- Dependencies: raw.subscriptions

with source as (
    select * from {{ source('raw', 'subscriptions') }}
),

final as (
    select
        cast(subscription_id as varchar) as subscription_id,
        cast(subscriber_id as varchar) as subscriber_id,
        cast(plan_type as varchar) as plan_type,
        cast(plan_price_usd as decimal(10,2)) as plan_price_usd,
        cast(billing_cycle as varchar) as billing_cycle,
        cast(status as varchar) as status,
        cast(start_date as date) as start_date,
        cast(end_date as date) as end_date,
        cast(mrr_usd as decimal(10,2)) as mrr_usd,
        cast(is_trial as boolean) as is_trial,
        cast(payment_method as varchar) as payment_method,
        cast(cancellation_reason as varchar) as cancellation_reason,
        current_timestamp() as _loaded_at
    from source
)

select * from final
