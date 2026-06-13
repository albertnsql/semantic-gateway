-- Model: dim_dates
-- Layer: marts
-- Grain: One row per day
-- Dependencies: None

with date_spine as (
    select dateadd(day, seq4(), '2022-01-01'::date) as date_day
    from table(generator(rowcount => 1096)) -- 3 years of days (2022 to 2024 inclusive)
),

final as (
    select
        date_day,
        date_trunc('week', date_day) as week_start,
        date_trunc('month', date_day) as month_start,
        date_trunc('quarter', date_day) as quarter_start,
        date_trunc('year', date_day) as year_start,
        dayofweek(date_day) as day_of_week,
        dayname(date_day) as day_name,
        monthname(date_day) as month_name,
        case when dayofweek(date_day) in (0, 6) then true else false end as is_weekend,
        case when date_day = last_day(date_day, 'month') then true else false end as is_month_end,
        quarter(date_day) as fiscal_quarter
    from date_spine
)

select * from final
