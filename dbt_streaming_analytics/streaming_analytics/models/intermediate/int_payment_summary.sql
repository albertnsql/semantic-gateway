-- Model: int_payment_summary
-- Layer: intermediate
-- Grain: One row per subscriber per payment month
-- Dependencies: stg_payments

with payments as (
    select * from {{ ref('stg_payments') }}
),

monthly_payments as (
    select
        subscriber_id,
        date_trunc('month', payment_date) as payment_month,
        sum(case when status = 'succeeded' then amount_usd else 0 end) as total_paid_usd,
        count(case when status = 'succeeded' then payment_id end) as successful_payments,
        count(case when status = 'failed' then payment_id end) as failed_payments,
        max(case when discount_applied = true then 1 else 0 end) = 1 as has_discount
    from payments
    group by 1, 2
),

final as (
    select
        subscriber_id,
        payment_month,
        total_paid_usd,
        successful_payments,
        failed_payments,
        case 
            when (successful_payments + failed_payments) > 0 
            then failed_payments::float / (successful_payments + failed_payments)
            else 0 
        end as payment_failure_rate,
        has_discount
    from monthly_payments
)

select * from final
