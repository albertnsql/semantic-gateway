"""
backend/evals/run_evals.py — Offline accuracy harness for the IntentExtractor.

Runs every question in golden_set.json through the REAL IntentExtractor
(using credentials from gateway/.env) and scores each result against the
pinned expected values.

Usage (from the project root):
    python backend/evals/run_evals.py

Options:
    --snapshot          Write a dated JSON results file to backend/evals/snapshots/
    --fail-under N      Exit code 1 if pass rate < N% (default: 80)
    --category CATEGORY Run only cases matching this category
    --verbose           Print full intent JSON for every case (not just failures)

Examples:
    python backend/evals/run_evals.py --snapshot
    python backend/evals/run_evals.py --category hallucination_resistance --verbose
    python backend/evals/run_evals.py --fail-under 90
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

# Force UTF-8 on Windows terminals to prevent cp1252 crashes from Unicode
# characters in notes fields or box-drawing output.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Path bootstrap — makes gateway/ importable from the project root
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent          # backend/evals/
_PROJECT_ROOT = _HERE.parent.parent              # Streaming_Analytics/
_GATEWAY_ROOT = _PROJECT_ROOT / "gateway"

if str(_GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_ROOT))

# config.py has a module-level `settings = Settings()` which resolves .env
# relative to the working directory.  We temporarily chdir to gateway/ so
# the import succeeds regardless of where the script is invoked from.
_orig_cwd = Path.cwd()
import os as _os
_os.chdir(_GATEWAY_ROOT)
try:
    from config import Settings                        # noqa: E402
    from core.intent_extractor import IntentExtractor  # noqa: E402
finally:
    _os.chdir(_orig_cwd)

logging.basicConfig(
    level=logging.WARNING,   # suppress gateway debug noise during evals
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("run_evals")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_GOLDEN_SET_PATH = _HERE / "golden_set.json"
_SNAPSHOTS_DIR = _HERE / "snapshots"

# A fixed, deterministic SUBSET (10 of the 18 certified metrics) used for evals.
# Kept here rather than loading from the live registry so eval results stay stable
# even as the registry YAML files change. NOTE: this is intentionally a curated
# subset, NOT the full metric set — if you want full production-universe coverage,
# expand this list (plus _CERTIFIED_DIMENSIONS / _CERTIFIED_TIME_GRAINS) and re-run
# the eval, since the pass rate may shift as more candidate metrics are exposed.
_CERTIFIED_METRICS = [
    "mrr",
    "expansion_mrr",
    "ltv",
    "engagement_rate",
    "churn_rate",
    "total_subscribers",
    "churned_subscribers",
    "recommendation_ctr",
    "clicked_recommendations",
    "total_recommendations",
]

_CERTIFIED_DIMENSIONS: dict[str, list[str]] = {
    "mrr":                   ["plan_type", "billing_cycle", "mrr_type", "period_month"],
    "expansion_mrr":         ["plan_type", "billing_cycle", "mrr_type"],
    "ltv":                   ["payment_method", "currency", "acquisition_channel", "payment_date"],
    "engagement_rate":       ["device_type", "quality_streamed", "referral_source", "session_start"],
    "churn_rate":            ["country", "plan_type", "acquisition_channel", "age_group"],
    "total_subscribers":     ["country", "plan_type", "acquisition_channel", "signup_date"],
    "churned_subscribers":   ["country", "plan_type", "acquisition_channel"],
    "recommendation_ctr":    ["event_date", "referral_source"],
    "clicked_recommendations": ["event_date", "referral_source"],
    "total_recommendations": ["event_date", "referral_source"],
}

_CERTIFIED_TIME_GRAINS: dict[str, dict[str, list[str]]] = {
    "mrr":           {"period_month": ["day", "week", "month", "quarter", "year"]},
    "expansion_mrr": {"period_month": ["month", "quarter"]},
    "ltv":           {"payment_date": ["day", "week", "month"]},
    "engagement_rate": {"session_start": ["day", "week"]},
    "churn_rate":    {"period_month": ["month", "quarter"]},
    "total_subscribers": {"signup_date": ["day", "week", "month"]},
    "churned_subscribers": {"signup_date": ["day", "week", "month"]},
    "recommendation_ctr": {"event_date": ["day", "week", "month"]},
}


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _score_case(case: dict, intent) -> dict:
    """
    Compare one extracted intent against its golden expected values.

    Returns a result dict with:
        passed  — True if all checked fields matched
        checks  — list of {field, expected, actual, ok} dicts
        partial — True if the case was marked partial_match_ok and only
                  soft fields failed
    """
    expected = case["expected"]
    partial_ok = case.get("partial_match_ok", False)
    checks: list[dict] = []
    hard_fail = False

    # ── Metrics ───────────────────────────────────────────────────────────────
    exp_metrics = set(expected.get("metrics", []))
    got_metrics = set(intent.metrics)
    metrics_ok = exp_metrics == got_metrics or (
        partial_ok and exp_metrics.issubset(got_metrics)
    )
    if not metrics_ok and exp_metrics:  # empty expected metrics = don't score
        hard_fail = True
    checks.append({
        "field": "metrics",
        "expected": sorted(exp_metrics),
        "actual": sorted(got_metrics),
        "ok": metrics_ok,
    })

    # ── Dimensions ────────────────────────────────────────────────────────────
    if "dimensions" in expected:
        exp_dims = set(expected["dimensions"])
        got_dims = set(intent.dimensions)
        dims_ok = exp_dims.issubset(got_dims)   # subset: extra dims are OK
        if not dims_ok and exp_dims:
            if not partial_ok:
                hard_fail = True
        checks.append({
            "field": "dimensions",
            "expected": sorted(exp_dims),
            "actual": sorted(got_dims),
            "ok": dims_ok,
        })

    # ── Time range (relative label) ───────────────────────────────────────────
    if "time_range_relative" in expected:
        exp_rel = expected["time_range_relative"]
        got_rel = intent.time_range.relative if intent.time_range else None
        rel_ok = exp_rel == got_rel
        if not rel_ok and exp_rel is not None and not partial_ok:
            hard_fail = True
        checks.append({
            "field": "time_range_relative",
            "expected": exp_rel,
            "actual": got_rel,
            "ok": rel_ok,
        })

    # ── Absolute date range ───────────────────────────────────────────────────
    if "time_range_start" in expected:
        exp_start = expected["time_range_start"]
        got_start = intent.time_range.start_date if intent.time_range else None
        start_ok = got_start == exp_start
        if not start_ok:
            hard_fail = True
        checks.append({
            "field": "time_range_start",
            "expected": exp_start,
            "actual": got_start,
            "ok": start_ok,
        })

    if "time_range_end" in expected:
        exp_end = expected["time_range_end"]
        got_end = intent.time_range.end_date if intent.time_range else None
        end_ok = got_end == exp_end
        if not end_ok:
            hard_fail = True
        checks.append({
            "field": "time_range_end",
            "expected": exp_end,
            "actual": got_end,
            "ok": end_ok,
        })

    # ── Aggregation level ─────────────────────────────────────────────────────
    if "aggregation_level" in expected:
        exp_agg = expected["aggregation_level"]
        got_agg = intent.aggregation_level
        agg_ok = exp_agg == got_agg
        if not agg_ok and not partial_ok:
            hard_fail = True
        checks.append({
            "field": "aggregation_level",
            "expected": exp_agg,
            "actual": got_agg,
            "ok": agg_ok,
        })

    # ── Needs clarification ───────────────────────────────────────────────────
    if "needs_clarification" in expected:
        exp_nc = expected["needs_clarification"]
        got_nc = intent.needs_clarification
        nc_ok = exp_nc == got_nc
        if not nc_ok:
            hard_fail = True
        checks.append({
            "field": "needs_clarification",
            "expected": exp_nc,
            "actual": got_nc,
            "ok": nc_ok,
        })

    # ── Filters (presence check only) ────────────────────────────────────────
    if "filters" in expected:
        exp_filters = expected["filters"]
        got_filters = [
            {"column": f.column, "operator": f.operator, "value": f.value}
            for f in intent.filters
        ]
        # Check each expected filter exists in got_filters
        filters_ok = all(ef in got_filters for ef in exp_filters)
        if not filters_ok and not partial_ok:
            hard_fail = True
        checks.append({
            "field": "filters",
            "expected": exp_filters,
            "actual": got_filters,
            "ok": filters_ok,
        })

    passed = not hard_fail
    return {
        "passed": passed,
        "checks": checks,
        "partial_match_ok": partial_ok,
    }


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------
_GREEN = "\033[32m"
_RED   = "\033[31m"
_YELLOW = "\033[33m"
_BOLD  = "\033[1m"
_RESET = "\033[0m"

def _tick(ok: bool) -> str:
    return f"{_GREEN}[ok]{_RESET}" if ok else f"{_RED}[!!]{_RESET}"


def _print_case_result(case: dict, result: dict, intent, elapsed: float, verbose: bool) -> None:
    status = f"{_GREEN}PASS{_RESET}" if result["passed"] else f"{_RED}FAIL{_RESET}"
    print(f"\n  [{status}] {case['id']} - {case['question'][:80]}  ({elapsed:.1f}s)")

    if not result["passed"] or verbose:
        for chk in result["checks"]:
            print(f"       {_tick(chk['ok'])} {chk['field']:25s}  "
                  f"expected={chk['expected']}  got={chk['actual']}")

    if not result["passed"] and case.get("notes"):
        print(f"       {_YELLOW}note:{_RESET} {case['notes']}")

    if verbose and intent:
        print(f"       raw_llm: {intent.raw_llm_response[:120]}")


# ---------------------------------------------------------------------------
# Main eval runner
# ---------------------------------------------------------------------------

def run_evals(
    category_filter: str | None = None,
    snapshot: bool = False,
    fail_under: int = 80,
    verbose: bool = False,
) -> int:
    """
    Load golden set, run every case through the real IntentExtractor,
    score results, print report, optionally write snapshot.

    Returns the exit code (0 = pass, 1 = fail).
    """
    # Load golden set
    with _GOLDEN_SET_PATH.open(encoding="utf-8") as fh:
        golden_set: list[dict] = json.load(fh)

    if category_filter:
        golden_set = [c for c in golden_set if c.get("category") == category_filter]
        if not golden_set:
            print(f"No cases found for category '{category_filter}'.")
            return 1

    # Boot the extractor with real credentials from gateway/.env
    print(f"\n{_BOLD}Streaming Analytics - Intent Extraction Eval{_RESET}")
    print(f"Golden set : {_GOLDEN_SET_PATH.name}  ({len(golden_set)} cases)")
    print(f"Snapshot   : {'yes' if snapshot else 'no'}")
    print(f"Fail under : {fail_under}%")
    if category_filter:
        print(f"Category   : {category_filter}")
    print()

    # `settings` singleton was loaded from gateway/.env at import time
    # (we chdir'd to gateway/ before importing config). Use it directly.
    from config import settings  # noqa: E402 — already cached, no re-read
    extractor = IntentExtractor(settings)

    results: list[dict] = []
    passed = 0
    failed = 0
    errored = 0

    print("-" * 70)

    for case in golden_set:
        start = time.monotonic()
        intent = None
        error_msg = None

        try:
            intent = extractor.extract(
                query=case["question"],
                available_metrics=_CERTIFIED_METRICS,
                available_dimensions=_CERTIFIED_DIMENSIONS,
                available_time_grains=_CERTIFIED_TIME_GRAINS,
            )
            result = _score_case(case, intent)
        except Exception as exc:
            error_msg = str(exc)
            result = {
                "passed": False,
                "checks": [{"field": "error", "expected": None, "actual": error_msg, "ok": False}],
                "partial_match_ok": False,
            }
            errored += 1

        elapsed = time.monotonic() - start

        if result["passed"]:
            passed += 1
        else:
            if not error_msg:
                failed += 1

        _print_case_result(case, result, intent, elapsed, verbose)

        # Collect for snapshot
        results.append({
            "id": case["id"],
            "category": case["category"],
            "question": case["question"],
            "passed": result["passed"],
            "checks": result["checks"],
            "error": error_msg,
            "elapsed_s": round(elapsed, 2),
            "intent": {
                "metrics": intent.metrics if intent else None,
                "dimensions": intent.dimensions if intent else None,
                "time_range": intent.time_range.model_dump() if (intent and intent.time_range) else None,
                "aggregation_level": intent.aggregation_level if intent else None,
                "needs_clarification": intent.needs_clarification if intent else None,
                "filters": [
                    {"column": f.column, "operator": f.operator, "value": f.value}
                    for f in intent.filters
                ] if intent else [],
            },
        })

        # Polite rate limiting between calls
        time.sleep(0.5)

    # Summary
    total = len(golden_set)
    pass_rate = (passed / total * 100) if total else 0
    print("-" * 70)
    print(f"{_BOLD}Results{_RESET}  {passed}/{total} passed  "
          f"({pass_rate:.1f}%)  "
          f"{failed} failed  {errored} errored")

    if pass_rate >= fail_under:
        print(f"{_GREEN}[ok] Pass rate {pass_rate:.1f}% >= threshold {fail_under}%{_RESET}")
        exit_code = 0
    else:
        print(f"{_RED}✗ Pass rate {pass_rate:.1f}% < threshold {fail_under}%{_RESET}")
        exit_code = 1

    # Optional snapshot
    if snapshot:
        _SNAPSHOTS_DIR.mkdir(exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        snap_path = _SNAPSHOTS_DIR / f"eval_{ts}.json"
        payload = {
            "run_at": datetime.utcnow().isoformat() + "Z",
            "snapshot_date": date.today().isoformat(),
            "total": total,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "pass_rate_pct": round(pass_rate, 2),
            "fail_threshold_pct": fail_under,
            "category_filter": category_filter,
            "cases": results,
        }
        with snap_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        print(f"\nSnapshot written → {snap_path}")

    return exit_code


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Offline accuracy eval for the streaming-analytics IntentExtractor."
    )
    p.add_argument(
        "--snapshot",
        action="store_true",
        help="Write a dated JSON results file to backend/evals/snapshots/",
    )
    p.add_argument(
        "--fail-under",
        type=int,
        default=80,
        metavar="N",
        help="Exit code 1 if pass rate < N%% (default: 80)",
    )
    p.add_argument(
        "--category",
        default=None,
        metavar="CATEGORY",
        help="Run only cases matching this category",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print full intent JSON for every case, not just failures",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(
        run_evals(
            category_filter=args.category,
            snapshot=args.snapshot,
            fail_under=args.fail_under,
            verbose=args.verbose,
        )
    )
