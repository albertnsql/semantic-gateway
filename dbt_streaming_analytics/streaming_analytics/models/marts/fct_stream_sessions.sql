-- Model: fct_stream_sessions
-- Layer: marts
-- Grain: One row per stream session
-- Dependencies: stg_stream_sessions, dim_subscribers, dim_dates,
--               stg_content_genre_bridge, stg_content_catalog

with stream_sessions as (
    select * from {{ ref('stg_stream_sessions') }}
),

subscribers as (
    select * from {{ ref('dim_subscribers') }}
),

-- Genre comes from TWO sources because sessions span two content id spaces, a
-- consequence of the dataset being generated in stages: everything through
-- 2026-05 uses bridge ids (91.0% of session rows), the 2026-07 append uses
-- catalog ids (8.5%), and the two share no ids at all. Reading only dim_content
-- resolved 8.5%; reading only the bridge resolved 91.0%. They are disjoint, so a
-- coalesce is lossless and reaches 99.50%.
content_genres as (
    select content_id, genre
    from {{ ref('stg_content_genre_bridge') }}
    where is_primary = true
),

content_catalog as (
    select content_id, genre from {{ ref('stg_content_catalog') }}
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
        -- 'unknown' rather than NULL for the residual 0.5%: with it a genre
        -- breakdown sums to the true session total, without it those rows vanish
        -- from the breakdown and the numbers stop tying out.
        coalesce(cg.genre, cc.genre, 'unknown') as content_primary_genre,
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
    left join content_genres cg on s.content_id = cg.content_id
    left join content_catalog cc on s.content_id = cc.content_id
    left join dates d on s.session_start::date = d.date_day
)

select * from final
