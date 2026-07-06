import os
import snowflake.connector
from dotenv import load_dotenv

load_dotenv('gateway/.env')
ctx = snowflake.connector.connect(
    user=os.environ['SNOWFLAKE_USER'],
    password=os.environ['SNOWFLAKE_PASSWORD'],
    account=os.environ['SNOWFLAKE_ACCOUNT'],
    warehouse=os.environ['SNOWFLAKE_WAREHOUSE'],
    database=os.environ['SNOWFLAKE_DATABASE'],
    schema=os.environ['SNOWFLAKE_SCHEMA']
)
cs = ctx.cursor()

query = """
SELECT
    period_month,
    COUNT(CASE WHEN mrr_type = 'churned' THEN 1 END) as churned_count,
    COUNT(DISTINCT CASE WHEN mrr_type != 'inactive' THEN subscriber_id END) as sub_count,
    COUNT(CASE WHEN mrr_type = 'churned' THEN 1 END)::FLOAT /
    NULLIF(COUNT(DISTINCT CASE WHEN mrr_type != 'inactive' THEN subscriber_id END), 0) AS value
FROM streaming_analytics.marts.fct_mrr_monthly
WHERE period_month >= '2026-03-01'
GROUP BY 1
ORDER BY 1 DESC
"""
cs.execute(query)
for row in cs.fetchall():
    print(row)
