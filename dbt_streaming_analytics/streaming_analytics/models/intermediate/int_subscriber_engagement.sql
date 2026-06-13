-- Model: int_subscriber_engagement
-- Layer: intermediate
-- Grain: One row per subscriber per month
-- Dependencies: stg_stream_sessions, stg_content_catalog, stg_content_genre_bridge

with stream_sessions as (
    select * from {{ ref('stg_stream_sessions') }}
),

content as (
    select * from {{ ref('stg_content_catalog') }}
),

content_genres as (
    select content_id, genre
    from {{ ref('stg_content_genre_bridge') }}
    where is_primary = true
),

session_metrics as (
    select
        s.subscriber_id,
        date_trunc('month', s.session_start::date) as activity_month,
        s.session_id,
        s.duration_minutes,
        s.completion_pct,
        s.content_id,
        s.device_type,
        cg.genre as primary_genre
    from stream_sessions s
    left join content_genres cg on s.content_id = cg.content_id
),

monthly_aggregation as (
    select
        subscriber_id,
        activity_month,
        count(session_id) as total_sessions,
        sum(duration_minutes) as total_watch_minutes,
        avg(completion_pct) as avg_completion_pct,
        count(distinct content_id) as unique_titles_watched,
        mode(device_type) as primary_device,
        mode(primary_genre) as primary_genre_watched
    from session_metrics
    group by 1, 2
)

select * from monthly_aggregation
