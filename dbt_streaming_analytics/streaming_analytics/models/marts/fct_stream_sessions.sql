-- Model: fct_stream_sessions
-- Layer: marts
-- Grain: One row per stream session
-- Dependencies: stg_stream_sessions, dim_subscribers, dim_content, dim_dates

with stream_sessions as (
    select * from {{ ref('stg_stream_sessions') }}
),

subscribers as (
    select * from {{ ref('dim_subscribers') }}
),

content as (
    select * from {{ ref('dim_content') }}
),

dates as (
    select * from {{ ref('dim_dates') }}
),

final as (
    select
        s.session_id,
        s.subscriber_id,
        s.content_id,
        s.session_start,
        s.session_end,
        s.duration_minutes,
        s.content_runtime_min,
        s.completion_pct,
        s.device_type,
        s.country,
        s.quality_streamed,
        s.buffering_events,
        s.was_resumed,
        s.referral_source,
        sub.cohort_month as subscriber_cohort_month,
        c.primary_genre as content_primary_genre,
        sub.plan_type,
        sub.age_group,
        sub.acquisition_channel,
        sub.subscription_status,
        sub.churn_reason,
        sub.signup_date,
        sub.churn_date,
        case when s.completion_pct >= 0.90 then true else false end as is_completed,
        case 
            when s.quality_streamed = '4K' then 'premium'
            when s.quality_streamed = 'HD' then 'standard'
            else 'basic'
        end as watch_quality_tier
    from stream_sessions s
    left join subscribers sub on s.subscriber_id = sub.subscriber_id
    left join content c on s.content_id = c.content_id
    left join dates d on s.session_start::date = d.date_day
)

select * from final
