-- Model: stg_user_watchlists
-- Layer: staging
-- Grain: One row per watchlist item
-- Dependencies: raw.user_watchlists

with source as (
    select * from {{ source('raw', 'user_watchlists') }}
),

final as (
    select
        cast(watchlist_id as varchar) as watchlist_id,
        cast(subscriber_id as varchar) as subscriber_id,
        cast(content_id as varchar) as content_id,
        cast(added_timestamp as {{ dbt.type_timestamp() }}) as added_timestamp,
        cast(removed_timestamp as {{ dbt.type_timestamp() }}) as removed_timestamp,
        cast(was_streamed as boolean) as was_streamed,
        cast(stream_session_id as varchar) as stream_session_id,
        cast(source as varchar) as source,
        {{ dbt.current_timestamp() }} as _loaded_at
    from source
)

select * from final
