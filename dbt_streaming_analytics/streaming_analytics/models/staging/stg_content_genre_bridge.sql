-- Model: stg_content_genre_bridge
-- Layer: staging
-- Grain: One row per content and genre bridge tag
-- Dependencies: raw.content_genre_bridge

with source as (
    select * from {{ source('raw', 'content_genre_bridge') }}
),

final as (
    select
        cast(bridge_id as varchar) as bridge_id,
        cast(content_id as varchar) as content_id,
        cast(genre as varchar) as genre,
        cast(is_primary as boolean) as is_primary,
        cast(tag_type as varchar) as tag_type,
        current_timestamp() as _loaded_at
    from source
)

select * from final
