import os
import snowflake.connector

con = snowflake.connector.connect(
    user=os.getenv('SNOWFLAKE_USER'),
    password=os.getenv('SNOWFLAKE_PASSWORD'),
    account=os.getenv('SNOWFLAKE_ACCOUNT'),
    warehouse='COMPUTE_WH',
    database='STREAMING_ANALYTICS',
    schema='marts'
)

cur = con.cursor()
cur.execute("""
SELECT plan_type, SUM(mrr_usd) AS mrr
FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly
WHERE is_active = TRUE
GROUP BY 1
ORDER BY 2 DESC
""")
print("mrr_by_plan:", cur.fetchall())
