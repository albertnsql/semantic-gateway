-- Model: metricflow_time_spine
-- Layer: marts
-- Grain: One row per day

with days as (
    select dateadd(day, seq4(), '2020-01-01'::date) as date_day
    from table(generator(rowcount => 3650))
),

final as (
    select cast(date_day as date) as date_day
    from days
)

select * from final
