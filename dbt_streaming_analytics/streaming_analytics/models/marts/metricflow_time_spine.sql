-- Model: metricflow_time_spine
-- Layer: marts
-- Grain: One row per day

with days as (
    {{ date_series('2020-01-01', 3650, 'day', 'date_day') }}
),

final as (
    select cast(date_day as date) as date_day
    from days
)

select * from final
