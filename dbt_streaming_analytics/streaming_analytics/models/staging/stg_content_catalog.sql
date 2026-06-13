-- Model: stg_content_catalog
-- Layer: staging
-- Grain: One row per content item
-- Dependencies: raw.content_catalog

with source as (
    select * from {{ source('raw', 'content_catalog') }}
),

final as (
    select
        cast(content_id as varchar) as content_id,
        cast(title as varchar) as title,
        cast(content_type as varchar) as content_type,
        cast(genre as varchar) as genre,
        cast(subgenre as varchar) as subgenre,
        cast(release_year as int) as release_year,
        cast(original_language as varchar) as original_language,
        cast(is_original as boolean) as is_original,
        cast(maturity_rating as varchar) as maturity_rating,
        cast(avg_runtime_minutes as int) as avg_runtime_minutes,
        cast(season_count as int) as season_count,
        cast(episode_count as int) as episode_count,
        cast(director as varchar) as director,
        cast(production_country as varchar) as production_country,
        cast(date_added_platform as date) as date_added_platform,
        current_timestamp() as _loaded_at
    from source
)

select * from final
