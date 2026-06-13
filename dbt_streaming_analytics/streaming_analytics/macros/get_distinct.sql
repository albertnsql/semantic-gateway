{% macro get_distinct(model_name, column_name) %}
    {% set query %}
        select distinct {{ column_name }} from {{ ref(model_name) }}
    {% endset %}
    {% if execute %}
        {% set results = run_query(query) %}
        {% do results.print_table() %}
    {% endif %}
{% endmacro %}
