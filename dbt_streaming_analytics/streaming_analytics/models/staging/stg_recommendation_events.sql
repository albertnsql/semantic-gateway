-- Model: stg_recommendation_events
-- Layer: staging
-- Grain: One row per recommendation event
-- Dependencies: raw.recommendation_events

with source as (
    select * from {{ source('raw', 'recommendation_events') }}
),

final as (
    select
        cast(event_id as varchar) as event_id,
        cast(subscriber_id as varchar) as subscriber_id,
        cast(content_id as varchar) as content_id,
        cast(event_timestamp as timestamp_ntz) as event_timestamp,
        cast(recommendation_type as varchar) as recommendation_type,
        cast(position_shown as int) as position_shown,
        cast(was_clicked as boolean) as was_clicked,
        cast(was_streamed as boolean) as was_streamed,
        cast(session_id as varchar) as session_id,
        cast(algorithm_version as varchar) as algorithm_version,
        current_timestamp() as _loaded_at
    from source
)

select * from final
