-- Model: stg_search_events
-- Layer: staging
-- Grain: One row per search event
-- Dependencies: raw.search_events

with source as (
    select * from {{ source('raw', 'search_events') }}
),

final as (
    select
        cast(search_id as varchar) as search_id,
        cast(subscriber_id as varchar) as subscriber_id,
        cast(search_timestamp as timestamp_ntz) as search_timestamp,
        cast(query_text as varchar) as query_text,
        cast(query_type as varchar) as query_type,
        cast(results_returned as int) as results_returned,
        cast(clicked_position as int) as clicked_position,
        cast(content_id_clicked as varchar) as content_id_clicked,
        cast(session_started as boolean) as session_started,
        cast(device_type as varchar) as device_type,
        current_timestamp() as _loaded_at
    from source
)

select * from final
