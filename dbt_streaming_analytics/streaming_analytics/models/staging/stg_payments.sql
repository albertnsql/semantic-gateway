-- Model: stg_payments
-- Layer: staging
-- Grain: One row per payment
-- Dependencies: raw.payments

with source as (
    select * from {{ source('raw', 'payments') }}
),

final as (
    select
        cast(payment_id as varchar) as payment_id,
        cast(subscription_id as varchar) as subscription_id,
        cast(subscriber_id as varchar) as subscriber_id,
        cast(payment_date as date) as payment_date,
        cast(billing_period_start as date) as billing_period_start,
        cast(billing_period_end as date) as billing_period_end,
        cast(amount_usd as decimal(10,2)) as amount_usd,
        cast(currency as varchar) as currency,
        cast(amount_local as decimal(10,2)) as amount_local,
        cast(status as varchar) as status,
        cast(failure_reason as varchar) as failure_reason,
        cast(payment_method as varchar) as payment_method,
        cast(stripe_charge_id as varchar) as stripe_charge_id,
        cast(is_renewal as boolean) as is_renewal,
        cast(discount_applied as boolean) as discount_applied,
        cast(discount_pct as decimal(5,4)) as discount_pct,
        current_timestamp() as _loaded_at
    from source
)

select * from final
