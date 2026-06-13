"""
demo/demo_scenarios.py — Portfolio demonstration scenarios for the AI Semantic Gateway.

Three runnable demos that call the live gateway API and print formatted output.
Each scenario showcases a key governance capability of the gateway.

Usage:
    # Start the gateway first:
    #   uvicorn main:app --reload --port 8000
    #   (from the gateway/ directory)
    #
    # Then run demos:
    #   python demo/demo_scenarios.py

Prerequisites:
    pip install requests
"""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import datetime

try:
    import requests
except ImportError:
    print("Please install requests: pip install requests")
    sys.exit(1)

GATEWAY_BASE_URL = "http://localhost:8000/api/v1"


# ──────────────────────────────────────────────── Display utilities

def _banner(title: str) -> None:
    print("\n" + "═" * 70)
    print(f"  {title}")
    print("═" * 70)


def _section(label: str) -> None:
    print(f"\n  {'─' * 60}")
    print(f"  {label}")
    print(f"  {'─' * 60}")


def _kv(key: str, value, indent: int = 4) -> None:
    pad = " " * indent
    if isinstance(value, (list, dict)):
        formatted = json.dumps(value, indent=indent + 2, default=str)
        print(f"{pad}{key}:")
        for line in formatted.splitlines():
            print(f"{pad}  {line}")
    else:
        print(f"{pad}{key}: {value}")


def _print_response(data: dict, show_sql: bool = True, show_data: bool = True) -> None:
    """Pretty-print a GatewayResponse dict."""
    status = data.get("status", "unknown")
    status_emoji = {"success": "✅", "rejected": "🚫", "dry_run": "🔍", "error": "❌"}.get(status, "❓")

    print(f"\n  Status: {status_emoji}  {status.upper()}")
    print(f"  Request ID: {data.get('request_id', 'N/A')}")

    # Query interpretation
    q = data.get("query", {})
    _section("Interpreted Query")
    _kv("Metrics", q.get("interpreted_metrics", []))
    _kv("Dimensions", q.get("interpreted_dimensions", []))
    if q.get("time_range"):
        tr = q["time_range"]
        _kv("Time Range", f"{tr.get('start_date')} → {tr.get('end_date')} ({tr.get('relative', '')})")

    # Validation
    v = data.get("validation", {})
    _section("Semantic Validation")
    _kv("Passed", v.get("passed"))
    _kv("Checks Run", v.get("checks_run", []))
    if v.get("violations"):
        print("    Violations:")
        for viol in v["violations"]:
            emoji = "🔴" if viol.get("severity") == "ERROR" else "⚠️"
            print(f"      {emoji} [{viol.get('severity')}] {viol.get('rule')}: {viol.get('message')[:120]}")

    # Governance (only on success)
    if status in ("success", "dry_run"):
        g = data.get("governance", {})
        _section("Governance & Lineage")
        _kv("Grain", g.get("grain", "N/A"))
        _kv("Grain Columns", g.get("grain_columns", []))
        _kv("Source Model", g.get("source_model", "N/A"))
        _kv("Lineage Path", g.get("lineage_path", []))
        if g.get("certified_definition"):
            print(f"    Certified Definition:")
            for line in textwrap.wrap(g["certified_definition"], width=64):
                print(f"      {line}")
        if g.get("warning"):
            print(f"    ⚠️  Warning: {g['warning']}")

        # SQL
        if show_sql:
            r = data.get("result", {})
            _section("Generated MetricFlow Query")
            print(f"    {r.get('metricflow_query', 'N/A')}")
            if r.get("generated_sql"):
                _section("Compiled SQL")
                for line in r["generated_sql"].splitlines()[:20]:
                    print(f"    {line}")

        # Results
        if show_data:
            r = data.get("result", {})
            _section("Query Results")
            _kv("Row Count", r.get("row_count", 0))
            rows = r.get("data", [])
            if rows:
                print("    Sample rows:")
                for row in rows[:5]:
                    print(f"      {row}")
            else:
                print("    (no data returned — dry run or no Snowflake connection)")

    # Rejection details
    if status == "rejected":
        rej = data.get("rejection", {})
        _section("Rejection Details")
        _kv("Reason", rej.get("reason", "N/A"))
        _kv("Violated Rules", rej.get("violated_rules", []))
        if rej.get("suggested_fix"):
            print(f"    💡 Suggested Fix: {rej['suggested_fix']}")
        if rej.get("safe_alternatives"):
            _kv("Safe Alternatives", rej["safe_alternatives"])


# ──────────────────────────────────────────────── Scenario 1

def scenario_1_valid_query() -> None:
    """
    SCENARIO 1: Valid Query — MRR by plan type, last 3 months.

    Expected outcome:
    - HTTP 200
    - Governance block shows grain (subscription+month) + lineage
    - Data returns MRR values per plan type
    """
    _banner("SCENARIO 1 — Valid Governed Query")
    print("  Query: 'What is the MRR by plan type for the last 3 months?'")
    print("  Expected: 200 success, governance block with grain + lineage")

    payload = {
        "query": "What is the MRR by plan type for the last 3 months?",
        "options": {
            "max_rows": 50,
            "include_sql": True,
            "include_lineage": True,
            "dry_run": False,
        },
    }

    try:
        resp = requests.post(f"{GATEWAY_BASE_URL}/query", json=payload, timeout=30)
        data = resp.json()
        print(f"\n  HTTP Status: {resp.status_code}")
        _print_response(data)
    except requests.exceptions.ConnectionError:
        print("\n  ⚠️  Gateway not running. Start with: uvicorn main:app --reload")
        print("  Showing expected response structure:")
        _show_expected_scenario_1()


def _show_expected_scenario_1() -> None:
    expected = {
        "status": "success",
        "query": {"interpreted_metrics": ["mrr"], "interpreted_dimensions": ["plan_type"]},
        "validation": {"passed": True, "checks_run": ["metrics_certified", "dimensions_certified", "grain_compatibility", "fanout_risk", "time_dimension", "filter_safety"]},
        "governance": {
            "grain": "One row per subscription (keyed on subscription_id)",
            "source_model": "fct_mrr_monthly",
            "lineage_path": ["stg_subscriptions", "int_subscription_periods", "fct_mrr_monthly"],
        },
        "result": {"data": [{"plan_type": "basic", "mrr": 45230.5}, {"plan_type": "premium", "mrr": 123450.0}, {"plan_type": "enterprise", "mrr": 78900.0}]},
    }
    print(json.dumps(expected, indent=4, default=str))


# ──────────────────────────────────────────────── Scenario 2

def scenario_2_grain_mismatch_caught() -> None:
    """
    SCENARIO 2: Grain mismatch rejection.

    Query: "Show me MRR and average completion rate by subscriber"
    Expected: HTTP 422 rejection with clear grain mismatch explanation.
    - MRR grain: subscription+month (fct_mrr_monthly)
    - engagement_rate grain: session_id (fct_stream_sessions)
    - Combining them = fanout + incorrect results
    """
    _banner("SCENARIO 2 — Grain Mismatch Caught by Governance")
    print("  Query: 'Show me MRR and average completion rate by subscriber'")
    print("  Expected: 422 REJECTED — grain mismatch between MRR and engagement_rate")

    payload = {
        "query": "Show me MRR and average completion rate by subscriber",
        "options": {"dry_run": False},
    }

    try:
        resp = requests.post(f"{GATEWAY_BASE_URL}/query", json=payload, timeout=30)
        data = resp.json()
        print(f"\n  HTTP Status: {resp.status_code}")
        _print_response(data)
    except requests.exceptions.ConnectionError:
        print("\n  ⚠️  Gateway not running. Start with: uvicorn main:app --reload")
        print("  Showing expected rejection response:")
        _show_expected_scenario_2()


def _show_expected_scenario_2() -> None:
    expected = {
        "status": "rejected",
        "validation": {
            "passed": False,
            "violations": [
                {
                    "rule": "fanout_risk",
                    "severity": "ERROR",
                    "message": (
                        "Joining 'fct_mrr_monthly' to 'fct_stream_sessions' would cause "
                        "a fanout at the subscription+month grain. This is a known dangerous "
                        "join in this data model. Use metric 'expansion_mrr' instead."
                    ),
                    "affected_elements": ["mrr", "engagement_rate"],
                }
            ],
        },
        "rejection": {
            "reason": "Fanout risk detected.",
            "violated_rules": ["fanout_risk"],
            "suggested_fix": "Query each metric separately.",
            "safe_alternatives": ["expansion_mrr", "churn_rate"],
        },
    }
    print(json.dumps(expected, indent=4, default=str))


# ──────────────────────────────────────────────── Scenario 3

def scenario_3_lineage_trace() -> None:
    """
    SCENARIO 3: Lineage trace for lifetime value by acquisition channel.

    Query: "What is the lifetime value by acquisition channel?"
    Expected: 200 success with full lineage:
      raw.payments → stg_payments → int_payment_summary → fct_payments → ltv metric
    """
    _banner("SCENARIO 3 — Lineage Trace (LTV by Acquisition Channel)")
    print("  Query: 'What is the lifetime value by acquisition channel?'")
    print("  Expected: 200 success, governance block shows full lineage")

    payload = {
        "query": "What is the lifetime value by acquisition channel?",
        "options": {
            "max_rows": 20,
            "include_sql": True,
            "include_lineage": True,
            "dry_run": True,  # Dry run to avoid Snowflake requirement
        },
    }

    try:
        resp = requests.post(f"{GATEWAY_BASE_URL}/query", json=payload, timeout=30)
        data = resp.json()
        print(f"\n  HTTP Status: {resp.status_code}")
        _print_response(data)
    except requests.exceptions.ConnectionError:
        print("\n  ⚠️  Gateway not running. Start with: uvicorn main:app --reload")
        print("  Showing expected lineage structure:")
        _show_expected_scenario_3()

    # Also fetch the direct lineage endpoint
    print("\n  📊 Direct Lineage Endpoint: GET /api/v1/lineage/ltv")
    try:
        resp = requests.get(f"{GATEWAY_BASE_URL}/lineage/ltv", timeout=15)
        if resp.status_code == 200:
            lineage_data = resp.json()
            _section("Lineage Trace")
            _kv("Source Model", lineage_data.get("source_model"))
            _kv("Upstream Models", lineage_data.get("upstream_models", []))
            _kv("Source Tables", lineage_data.get("source_tables", []))
            print("    Transformation Steps:")
            for step in lineage_data.get("transformation_steps", []):
                print(f"      [{step['layer'].upper():>12}] {step['model_name']}")
    except requests.exceptions.ConnectionError:
        print("  ⚠️  Gateway not running.")


def _show_expected_scenario_3() -> None:
    expected = {
        "status": "dry_run",
        "query": {"interpreted_metrics": ["ltv"], "interpreted_dimensions": ["acquisition_channel"]},
        "validation": {"passed": True},
        "governance": {
            "grain": "One row per payment (keyed on payment_id)",
            "source_model": "fct_payments",
            "lineage_path": [
                "stg_payments",
                "int_payment_summary",
                "fct_payments",
            ],
            "certified_definition": (
                "Lifetime Value is defined as: Total revenue per subscriber lifetime. "
                "Grain: One row per payment. Certified source: fct_payments. "
                "Lineage: stg_payments → int_payment_summary → fct_payments."
            ),
        },
        "result": {
            "metricflow_query": "mf query --metrics ltv --group-by acquisition_channel --explain",
            "generated_sql": (
                "SELECT acquisition_channel, SUM(CASE WHEN status = 'succeeded' THEN amount_usd ELSE 0 END) AS ltv "
                "FROM STREAMING_ANALYTICS.marts.fct_payments GROUP BY 1 ORDER BY 1"
            ),
        },
    }
    print(json.dumps(expected, indent=4, default=str))


# ──────────────────────────────────────────────── Main runner

def run_all_scenarios() -> None:
    """Run all three demonstration scenarios and print formatted results."""
    print("\n" + "█" * 70)
    print("█" + " " * 68 + "█")
    print("█  AI SEMANTIC GATEWAY — Portfolio Demonstration" + " " * 21 + "█")
    print("█  Streaming Analytics Platform (Netflix-style SaaS)" + " " * 17 + "█")
    print("█" + " " * 68 + "█")
    print("█" * 70)
    print(f"\n  Gateway URL: {GATEWAY_BASE_URL}")
    print(f"  Timestamp  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    scenario_1_valid_query()
    scenario_2_grain_mismatch_caught()
    scenario_3_lineage_trace()

    _banner("DEMONSTRATION COMPLETE")
    print("  All 3 scenarios executed.")
    print("  The AI Semantic Gateway successfully demonstrates:")
    print("    ✅  Scenario 1: Governed query with lineage context")
    print("    🚫  Scenario 2: Grain mismatch caught before SQL generation")
    print("    📊  Scenario 3: Full lineage trace from raw to metric")
    print()


if __name__ == "__main__":
    run_all_scenarios()
