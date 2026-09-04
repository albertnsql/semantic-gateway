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

final as (
    select
        c.content_id,
        c.title,
        c.content_type,
        -- From the catalog, which is this model's OWN spine. It previously came
        -- from stg_content_genre_bridge, LEFT JOINed on content_id -- but the bridge
        -- and the catalog were generated a month apart with fresh uuid4s and share
        -- ZERO content ids, so the join matched nothing and primary_genre was null
        -- for all 2,500 rows. stg_content_catalog.genre sits on the same row, is 0%
        -- null, and carries the identical 10-value vocabulary.
        c.genre as primary_genre,
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
)

select * from final
