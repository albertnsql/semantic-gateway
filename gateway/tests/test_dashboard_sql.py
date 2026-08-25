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

import datetime as _dt


import pytest

from api.routes.dashboard import (
    _ANCHOR_SQL,
    _DEFAULT_DATA_THROUGH,
    _WIDGET_ANCHOR,
    _WIDGET_SQL,
    _month_start,
    _resolve_anchor_date,
    _resolve_yoy_dates,
    _sql_mrr_bridge,
)

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


def _duckdb_ready() -> bool:
    """True when the DuckDB file exists and has marts built."""
    try:
        import os

        import duckdb

        from config import settings

        path = os.path.abspath(settings.duckdb_path)
        if not os.path.exists(path):
            return False
        con = duckdb.connect(path)
        try:
            return bool(
                con.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = 'marts'"
                ).fetchone()[0]
            )
        finally:
            con.close()
    except Exception:
        return False


def _warehouse_pool():
    """
    Build a pool for whichever warehouse is configured.

    The reconciliation assertions below are the only ones in this file with teeth,
    and their SQL is dialect-neutral (CASE, LAG, USING) — so the gate is about
    warehouse REACHABILITY, not about Snowflake specifically. Keeping it
    Snowflake-only is what silently disabled them when the trial expired.
    """
    from config import settings

    if settings.warehouse_engine.lower() == "duckdb":
        from core.duckdb_pool import DuckDBPool

        return DuckDBPool(settings=settings)

    from core.snowflake_pool import SnowflakePool

    return SnowflakePool(settings=settings, size=1)


def _warehouse_available() -> bool:
    """Whether the configured warehouse can actually answer a query."""
    try:
        from config import settings
    except Exception:
        return False
    if settings.warehouse_engine.lower() == "duckdb":
        return _duckdb_ready()
    return _HAS_SNOWFLAKE


_HAS_WAREHOUSE = _warehouse_available()

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


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="No warehouse reachable")
def test_bridge_components_reconcile_with_actual_mrr_change() -> None:
    """
    The components must sum to the real month-over-month change in active MRR.

    This is the assertion the widget never had. Under the old formula it failed in
    11 of 12 months, by up to $1,984 in a single month.
    """
    pool = _warehouse_pool()
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


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="No warehouse reachable")
def test_bridge_expansion_matches_the_certified_metric() -> None:
    """
    Expansion in the bridge must equal the `expansion_mrr` metric.

    These are the same concept defined twice — the widget in hand-written SQL, the
    metric in MetricFlow. They disagreed ($4,204.22 vs $1,738.06) because only the
    metric used the movement column.
    """
    pool = _warehouse_pool()
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


# ── Per-fact data-through anchors ─────────────────────────────────────────────
# Every widget shared one anchor: MAX(period_month) from fct_mrr_monthly. That fact
# is built from a date spine driven by current_date(), so it runs to the CURRENT
# calendar month, while the event facts stop at the last appended month. On
# 2026-08-18 fct_mrr_monthly reached 2026-08-01 and fct_stream_sessions ended
# 2026-06-30, so sessions_by_referral — the only widget using the anchor as the
# LOWER bound of a single-month window — asked for July 2026 and got nothing.
# An empty result is not an error to the frontend: parseChart() swaps in
# generateMockBar() and labels it "Estimated", so the dashboard showed invented
# Enterprise/Pro/Free bars where referral sources belong.


class TestWidgetAnchors:
    """Always-on guards on the widget -> fact mapping."""

    def test_session_widgets_use_the_session_anchor(self) -> None:
        for widget in (
            "sessions_trend",
            "sessions_by_referral",
            "watch_time_kpi",
            "engagement_kpi",
        ):
            assert _WIDGET_ANCHOR.get(widget) == "sessions", widget

    def test_revenue_widgets_use_the_payments_anchor(self) -> None:
        """revenue_kpi reads fct_payments, which also lags the MRR spine."""
        for widget in ("revenue_kpi", "mrr_kpi"):
            assert _WIDGET_ANCHOR.get(widget) == "payments", widget

    def test_mrr_backed_widgets_fall_through_to_the_mrr_anchor(self) -> None:
        """Absent from the map means 'mrr', which is the historical behaviour."""
        for widget in (
            "mrr_trend",
            "retention_trend",
            "mrr_bridge",
            "subs_kpi",
            "churn_rate_kpi",
            "net_mrr_growth_kpi",
            "sub_dist",
        ):
            assert _WIDGET_ANCHOR.get(widget, "mrr") == "mrr", widget

    def test_every_anchored_widget_exists(self) -> None:
        """A typo in _WIDGET_ANCHOR would silently leave that widget on the MRR anchor."""
        for widget in _WIDGET_ANCHOR:
            assert widget in _WIDGET_SQL, widget

    def test_each_anchor_queries_the_fact_it_names(self) -> None:
        assert "fct_mrr_monthly" in _ANCHOR_SQL["mrr"]
        assert "fct_stream_sessions" in _ANCHOR_SQL["sessions"]
        assert "fct_payments" in _ANCHOR_SQL["payments"]

    def test_event_anchors_truncate_to_a_month(self) -> None:
        """
        session_start / payment_date are timestamps. Without DATE_TRUNC the anchor
        would be a day-level date, and every window built as
        >= data_through_date would collapse to a single day.
        """
        for anchor in ("sessions", "payments"):
            assert "DATE_TRUNC('month'" in _ANCHOR_SQL[anchor], anchor


class TestResolveYoyDatesHonoursTheAnchor:
    """
    _resolve_yoy_dates accepted max_data_date and threw it away for the current
    year, returning the wall-clock _DEFAULT_DATA_THROUGH instead. 33 sites build
    windows from the result.
    """

    def test_current_year_anchor_wins(self) -> None:
        d = _resolve_yoy_dates([2026], "2026-06-01")
        assert d["data_through_date"] == "2026-06-01"

    def test_prior_year_window_tracks_the_anchor(self) -> None:
        """The YoY comparison must move with it, or it compares unequal spans."""
        d = _resolve_yoy_dates([2026], "2026-06-01")
        assert d["prior_year_equiv_end"] == "2025-06-01"

    def test_day_level_anchor_is_normalised_to_the_month(self) -> None:
        d = _resolve_yoy_dates([2026], "2026-06-30")
        assert d["data_through_date"] == "2026-06-01"

    def test_anchor_from_another_year_is_ignored(self) -> None:
        """A lagging anchor must not drag a 2026 selection back into 2025."""
        d = _resolve_yoy_dates([2026], "2025-11-01")
        assert d["data_through_date"] == _DEFAULT_DATA_THROUGH

    def test_completed_prior_year_still_uses_december(self) -> None:
        d = _resolve_yoy_dates([2025], "2026-07-01")
        assert d["data_through_date"] == "2025-12-01"

    def test_missing_anchor_falls_back(self) -> None:
        d = _resolve_yoy_dates([2026], "")
        assert d["data_through_date"] == _DEFAULT_DATA_THROUGH

    def test_in_progress_month_is_still_capped(self) -> None:
        """
        The cap is what keeps a partial month off the dashboard. An anchor inside
        the current calendar month must not survive it.
        """
        today = _dt.date.today()
        d = _resolve_yoy_dates([today.year], today.strftime("%Y-%m-%d"))
        assert d["data_through_date"] <= _DEFAULT_DATA_THROUGH


class TestMonthStart:
    def test_normalises_any_day(self) -> None:
        assert _month_start("2026-06-30") == "2026-06-01"

    def test_is_idempotent(self) -> None:
        assert _month_start("2026-06-01") == "2026-06-01"


class _PoolShim:
    """Minimal stand-in for SQLGenerator — _resolve_anchor_date only calls this."""

    def __init__(self, pool) -> None:
        self._pool = pool

    def execute_query(self, sql: str) -> list[dict]:
        return self._pool.execute(sql)


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="No warehouse reachable")
def test_session_anchor_lands_on_a_month_that_has_sessions() -> None:
    """
    The regression, end to end: resolve the anchor the way the route does, build
    sessions_by_referral from it, and require rows. Zero rows is what the frontend
    silently replaces with mock bars, so an empty result here IS the bug.
    """
    pool = _warehouse_pool()
    pool.initialise()
    try:
        shim = _PoolShim(pool)
        anchor_date = _resolve_anchor_date(shim, None, "sessions")
        sql = _WIDGET_SQL["sessions_by_referral"]([], [], [], anchor_date)
        rows = pool.execute(sql)
    finally:
        pool.close_all()

    assert rows, (
        f"sessions_by_referral returned no rows for anchor {anchor_date} — "
        "the frontend renders mock 'Estimated' bars in this case"
    )
    names = {str(r.get("NAME") or r.get("name")) for r in rows}
    # Plan tiers here would mean we are looking at generateMockBar output.
    assert not names & {"Free", "Pro", "Enterprise"}, f"mock-looking labels: {names}"


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="No warehouse reachable")
def test_every_anchor_resolves_to_a_completed_month_start() -> None:
    """
    The anchors legitimately agree once the event facts are caught up, so this does
    not assert they differ. It pins the two properties every consumer relies on:
    a month start, and never an in-progress month.
    """
    pool = _warehouse_pool()
    pool.initialise()
    try:
        shim = _PoolShim(pool)
        resolved = {a: _resolve_anchor_date(shim, None, a) for a in _ANCHOR_SQL}
    finally:
        pool.close_all()

    for anchor, value in resolved.items():
        assert value <= _DEFAULT_DATA_THROUGH, f"{anchor}={value} is not a completed month"
        assert value.endswith("-01"), f"{anchor}={value} is not a month start"


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="No warehouse reachable")
def test_no_active_subscribers_after_the_latest_active_period() -> None:
    """
    The invariant the snapshot-metric default leans on.

    `default_snapshot_time_range()` gives a snapshot metric the latest period that
    has active rows. MetricFlow then rounds the end of any range UP by one period,
    so a request for 2026-08 compiles to
    ``BETWEEN '2026-08-01' AND '2026-09-01'`` and would union two months.

    That is safe only because the trailing period is always churn-only:
    int_subscription_periods keeps a subscription active while
    ``period_month <= date_trunc('month', current_date())`` but emits cancellation
    rows a month PAST the end date, so the month after the newest active one holds
    rows with zero active subscribers.

    Structural rather than lucky, but subtle enough to deserve an assertion. If the
    date-spine logic ever changes, this fails here instead of silently inflating
    every "how many subscribers do we have" answer.
    """
    pool = _warehouse_pool()
    pool.initialise()
    try:
        rows = pool.execute(
            """
            WITH latest AS (
                SELECT MAX(period_month) AS p
                FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly
                WHERE is_active = TRUE
            )
            SELECT m.period_month AS period,
                   COUNT(DISTINCT CASE WHEN m.is_active THEN m.subscriber_id END) AS active
            FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly m, latest
            WHERE m.period_month > latest.p
            GROUP BY 1
            ORDER BY 1
            """
        )
    finally:
        pool.close_all()

    offenders = [
        f"{str(r.get('PERIOD') or r.get('period'))[:10]}: "
        f"{int(r.get('ACTIVE') or r.get('active') or 0)} active"
        for r in rows
        if int(r.get("ACTIVE") or r.get("active") or 0) > 0
    ]
    assert not offenders, (
        "A period after the latest active month has active subscribers, so the "
        "snapshot default would union two months and overstate the base:\n  "
        + "\n  ".join(offenders)
    )
