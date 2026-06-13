{% test assert_positive_mrr_when_active(model, column_name) %}

with validation as (
    select * from {{ model }}
),

validation_errors as (
    select *
    from validation
    where {{ column_name }} <= 0
      and is_active = true
)

select *
from validation_errors

{% endtest %}
