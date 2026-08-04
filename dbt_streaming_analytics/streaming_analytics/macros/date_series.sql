{#
    date_series — portable replacement for Snowflake's row generator.

    Three models built their date spines with Snowflake-only syntax:

        select dateadd(day, seq4(), '2020-01-01'::date) as date_day
        from table(generator(rowcount => 3650))

    `table(generator(...))` and `seq4()` do not exist outside Snowflake, which is
    what blocked metricflow_time_spine, dim_dates and int_subscription_periods on
    DuckDB. This macro emits the equivalent per adapter rather than hard-coding
    one dialect, so `DBT_TARGET=dev` (Snowflake) still builds if that account is
    ever revived.

    DuckDB uses `range(0, n)` plus interval multiplication (`i * INTERVAL 1 DAY`),
    which is the documented way to build a dynamic interval there — a bare
    `INTERVAL (i) DAY` only accepts a literal.

    Args:
        start_date: ISO date string, e.g. '2020-01-01'.
        count:      Number of rows/periods to generate.
        part:       'day' or 'month' — the step granularity.
        column:     Output column name.

    Emits a complete SELECT, so call it as the body of a CTE:

        with days as (
            {{ date_series('2020-01-01', 3650, 'day', 'date_day') }}
        )
#}

{% macro date_series(start_date, count, part='day', column='date_day') -%}
    {%- if target.type == 'duckdb' -%}
    select cast(date '{{ start_date }}' + (i * interval 1 {{ part }}) as date) as {{ column }}
    from range(0, {{ count }}) as t(i)
    {%- else -%}
    select cast(dateadd({{ part }}, seq4(), '{{ start_date }}'::date) as date) as {{ column }}
    from table(generator(rowcount => {{ count }}))
    {%- endif -%}
{%- endmacro %}
