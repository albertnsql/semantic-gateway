-- Model: int_content_engagement
-- Layer: intermediate
-- Grain: One row per content item
-- Dependencies: stg_stream_sessions

with stream_sessions as (
    select * from {{ ref('stg_stream_sessions') }}
),

content_aggregation as (
    select
        content_id,
        count(session_id) as total_streams,
        sum(duration_minutes) as total_watch_minutes,
        avg(completion_pct) as avg_completion_pct,
        count(distinct subscriber_id) as unique_subscribers
    from stream_sessions
    group by 1
),

final as (
    select
        content_id,
        total_streams,
        total_watch_minutes,
        avg_completion_pct,
        unique_subscribers,
        case
            when avg_completion_pct >= 0.70 then 'high'
            when avg_completion_pct >= 0.40 then 'medium'
            else 'low'
        end as completion_rate_tier
    from content_aggregation
)

select * from final
