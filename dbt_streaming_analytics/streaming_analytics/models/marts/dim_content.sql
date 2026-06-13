-- Model: dim_content
-- Layer: marts
-- Grain: One row per content item
-- Dependencies: stg_content_catalog, int_content_engagement, stg_content_genre_bridge

with content as (
    select * from {{ ref('stg_content_catalog') }}
),

engagement as (
    select * from {{ ref('int_content_engagement') }}
),

genres as (
    select content_id, genre as primary_genre
    from {{ ref('stg_content_genre_bridge') }}
    where is_primary = true
),

final as (
    select
        c.content_id,
        c.title,
        c.content_type,
        g.primary_genre,
        c.subgenre,
        c.is_original,
        c.maturity_rating,
        c.avg_runtime_minutes,
        c.release_year,
        coalesce(e.total_streams, 0) as total_streams,
        coalesce(e.avg_completion_pct, 0) as avg_completion_pct,
        coalesce(e.completion_rate_tier, 'low') as completion_rate_tier,
        coalesce(e.unique_subscribers, 0) as unique_subscribers,
        c.date_added_platform
    from content c
    left join engagement e on c.content_id = e.content_id
    left join genres g on c.content_id = g.content_id
)

select * from final
