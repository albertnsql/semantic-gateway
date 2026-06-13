-- Model: stg_subscribers
-- Layer: staging
-- Grain: One row per subscriber
-- Dependencies: raw.subscribers

with source as (
    select * from {{ source('raw', 'subscribers') }}
),

final as (
    select
        cast(subscriber_id as varchar) as subscriber_id,
        cast(email as varchar) as email,
        cast(country as varchar) as country,
        cast(signup_date as date) as signup_date,
        cast(acquisition_channel as varchar) as acquisition_channel,
        cast(plan_type as varchar) as plan_type,
        cast(plan_price_usd as decimal(10,2)) as plan_price_usd,
        cast(subscription_status as varchar) as subscription_status,
        cast(trial_start_date as date) as trial_start_date,
        cast(trial_end_date as date) as trial_end_date,
        cast(churn_date as date) as churn_date,
        cast(churn_reason as varchar) as churn_reason,
        cast(age_group as varchar) as age_group,
        cast(device_preference as varchar) as device_preference,
        current_timestamp() as _loaded_at
    from source
)

select * from final
