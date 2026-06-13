-- Model: fct_payments
-- Layer: marts
-- Grain: One row per payment
-- Dependencies: stg_payments, dim_subscribers, int_subscription_periods

with payments as (
    select * from {{ ref('stg_payments') }}
),

subscribers as (
    select * from {{ ref('dim_subscribers') }}
),

subscription_periods as (
    select * from {{ ref('int_subscription_periods') }}
),

ranked_payments as (
    select
        payment_id,
        row_number() over (partition by subscriber_id order by payment_date) as payment_num
    from payments
    where status = 'succeeded'
),

final as (
    select
        p.payment_id,
        p.subscription_id,
        p.subscriber_id,
        p.payment_date,
        p.billing_period_start,
        p.billing_period_end,
        p.amount_usd,
        p.currency,
        p.amount_local,
        p.status,
        p.failure_reason,
        p.payment_method,
        p.stripe_charge_id,
        p.is_renewal,
        p.discount_applied,
        p.discount_pct,
        s.cohort_month as subscriber_cohort_month,
        s.country,
        sp.plan_type,
        sp.billing_cycle,
        case when rp.payment_num = 1 then true else false end as is_first_payment,
        case when p.status = 'succeeded' then p.amount_usd else 0 end as mrr_contribution_usd
    from payments p
    left join subscribers s on p.subscriber_id = s.subscriber_id
    left join subscription_periods sp 
        on p.subscription_id = sp.subscription_id 
        and date_trunc('month', p.billing_period_start) = sp.period_month
    left join ranked_payments rp on p.payment_id = rp.payment_id
)

select * from final
