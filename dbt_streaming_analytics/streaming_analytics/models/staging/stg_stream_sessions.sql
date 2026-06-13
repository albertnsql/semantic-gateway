-- Model: stg_stream_sessions
-- Layer: staging
-- Grain: One row per stream session
-- Dependencies: raw.stream_sessions

with source as (
    select * from {{ source('raw', 'stream_sessions') }}
),

final as (
    select
        cast(session_id as varchar) as session_id,
        cast(subscriber_id as varchar) as subscriber_id,
        cast(content_id as varchar) as content_id,
        cast(session_start as timestamp_ntz) as session_start,
        cast(session_end as timestamp_ntz) as session_end,
        cast(duration_minutes as int) as duration_minutes,
        cast(content_runtime_min as int) as content_runtime_min,
        cast(completion_pct as decimal(5,4)) as completion_pct,
        cast(device_type as varchar) as device_type,
        cast(country as varchar) as country,
        cast(quality_streamed as varchar) as quality_streamed,
        cast(buffering_events as int) as buffering_events,
        cast(was_resumed as boolean) as was_resumed,
        cast(referral_source as varchar) as referral_source,
        current_timestamp() as _loaded_at
    from source
)

select * from final
