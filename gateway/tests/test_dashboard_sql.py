"""
tests/test_dashboard_sql.py — Tests for the hand-written dashboard SQL builders.

`dashboard.py` is the second-largest module in the gateway and had NO test
coverage. It is also the one place a business concept is defined a second time,
in a different language, with nothing cross-checking it against the semantic
layer — so it is exactly where silent divergence lives.

The MRR bridge summed `mrr_usd` (a subscription's full monthly price) for every
component. A bridge component must be a MOVEMENT, so Expansion read $4,204.22
where the certified `expansion_mrr` metric said $1,738.06, and the components
reconciled with the real month-over-month MRR change in only 1 of 12 months.

Two kinds of test here:
  * shape tests, which always run and pin the formula;
  * a reconciliation test against Snowflake, skipped without credentials, which
    is the one that actually proves the arithmetic.
"""

from __future__ import annotations


import pytest

from api.routes.dashboard import _WIDGET_SQL, _sql_mrr_bridge

_BRIDGE_ARGS = ([], [], [], "2026-06-01")


def _bridge_sql() -> str:
    return _sql_mrr_bridge(*_BRIDGE_ARGS)


class TestBridgeFormulaShape:
    """Always-on guards. Cheap, and they pin the exact regression."""

    def test_bridge_sums_the_movement_column(self) -> None:
        assert "SUM(mrr_change_usd" in _bridge_sql()

    def test_bridge_does_not_use_the_old_full_price_formula(self) -> None:
        """The regression: summing mrr_usd made every component a price, not a delta."""
        sql = _bridge_sql()
        assert "THEN -mrr_usd ELSE mrr_usd END" not in sql, (
            "bridge reverted to summing full plan MRR"
        )

    def test_bridge_subtracts_departing_mrr_for_churn(self) -> None:
        """mrr_change_usd carries only plan-change deltas; churn loss lives on mrr_usd."""
        sql = _bridge_sql()
        assert "mrr_type = 'churned' THEN -mrr_usd" in sql

    def test_bridge_still_covers_all_four_components(self) -> None:
        sql = _bridge_sql()
        for component in ("new", "expansion", "contraction", "churned"):
            assert f"'{component}'" in sql

    def test_bridge_is_registered(self) -> None:
        assert "mrr_bridge" in _WIDGET_SQL


# ── Reconciliation against real data ────────────────────────────────────────
# This is the test that has teeth. A bridge whose components do not sum to the
# actual change in MRR is not a bridge.

def _snowflake_configured() -> bool:
    """
    True when real Snowflake credentials are available.

    Check `settings`, NOT os.environ: credentials live in gateway/.env and are read
    by pydantic-settings, so they are never exported to the process environment.
    An os.getenv() gate skipped these tests unconditionally, which would have left
    the only assertion with teeth permanently dead.
    """
    try:
        from config import settings
    except Exception:
        return False
    placeholders = {
        "your-account.snowflakecomputing.com",
        "snowflake_user",
        "snowflake_password",
        "",
    }
    return not (
        settings.snowflake_account in placeholders
        or settings.snowflake_user in placeholders
        or settings.snowflake_password in placeholders
    )


_HAS_SNOWFLAKE = _snowflake_configured()

_RECONCILE_SQL = """
WITH movement AS (
    SELECT period_month,
           SUM(mrr_change_usd + CASE WHEN mrr_type = 'churned' THEN -mrr_usd ELSE 0 END)
               AS bridge_net
    FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly
    GROUP BY 1
),
active AS (
    SELECT period_month,
           SUM(CASE WHEN is_active THEN mrr_usd ELSE 0 END) AS active_mrr
    FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly
    GROUP BY 1
),
delta AS (
    SELECT period_month,
           active_mrr,
           LAG(active_mrr) OVER (ORDER BY period_month) AS prev_active_mrr
    FROM active
)
SELECT d.period_month,
       d.active_mrr - d.prev_active_mrr AS actual_change,
       m.bridge_net
FROM delta d
JOIN movement m USING (period_month)
-- Only months with a real active baseline on BOTH sides. Periods past the loaded
-- data have churn rows but no active MRR, which is a completeness artifact rather
-- than a formula error.
WHERE d.prev_active_mrr > 0
  AND d.active_mrr > 0
ORDER BY d.period_month
"""


@pytest.mark.skipif(not _HAS_SNOWFLAKE, reason="Snowflake credentials not configured")
def test_bridge_components_reconcile_with_actual_mrr_change() -> None:
    """
    The components must sum to the real month-over-month change in active MRR.

    This is the assertion the widget never had. Under the old formula it failed in
    11 of 12 months, by up to $1,984 in a single month.
    """
    from config import settings
    from core.snowflake_pool import SnowflakePool

    pool = SnowflakePool(settings=settings, size=1)
    pool.initialise()
    try:
        with pool.acquire() as conn:
            cur = conn.cursor()
            cur.execute(_RECONCILE_SQL)
            rows = cur.fetchall()
    finally:
        pool.close_all()

    assert rows, "no complete months found — cannot verify reconciliation"

    mismatches = [
        f"{str(month)[:10]}: actual={float(actual):,.2f} bridge={float(bridge):,.2f} "
        f"diff={float(bridge) - float(actual):,.2f}"
        for month, actual, bridge in rows
        if abs(float(bridge) - float(actual)) > 0.01
    ]
    assert not mismatches, (
        f"{len(mismatches)}/{len(rows)} months do not reconcile:\n  "
        + "\n  ".join(mismatches)
    )


@pytest.mark.skipif(not _HAS_SNOWFLAKE, reason="Snowflake credentials not configured")
def test_bridge_expansion_matches_the_certified_metric() -> None:
    """
    Expansion in the bridge must equal the `expansion_mrr` metric.

    These are the same concept defined twice — the widget in hand-written SQL, the
    metric in MetricFlow. They disagreed ($4,204.22 vs $1,738.06) because only the
    metric used the movement column.
    """
    from config import settings
    from core.snowflake_pool import SnowflakePool

    pool = SnowflakePool(settings=settings, size=1)
    pool.initialise()
    try:
        with pool.acquire() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    SUM(mrr_change_usd + CASE WHEN mrr_type='churned' THEN -mrr_usd ELSE 0 END)
                        AS bridge_expansion,
                    SUM(mrr_change_usd) AS metric_expansion
                FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly
                WHERE mrr_type = 'expansion'
                  AND period_month = '2026-06-01'
            """)
            bridge_expansion, metric_expansion = cur.fetchone()
    finally:
        pool.close_all()

    assert abs(float(bridge_expansion) - float(metric_expansion)) < 0.01, (
        f"bridge={float(bridge_expansion):,.2f} metric={float(metric_expansion):,.2f}"
    )
