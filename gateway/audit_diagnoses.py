"""
audit_diagnoses.py — snapshot the CONCLUSIONS a set of diagnoses reach.

Why this exists
---------------
Nothing catches a change that alters an answer. Over one week of building the
diagnostic path, four separate changes moved a conclusion and every one was caught
by reading output by hand:

* the prefix-map fix changed which semantic model 11 pairs resolved against
* the broad-based verdict replaced three weak partials with one summary line
* retiring the June-2026 artifact removed a warning from every June answer
* the baseline check added a line to any single-month diagnosis

Each was intended. The problem is that an UNintended one looks identical: the tests
pass, the audit still reports 267/267, and the answer quietly says something else.

What it is, and is not
----------------------
This is a REGRESSION harness, not a correctness one. It records what the agent
currently concludes and tells you when that changes. It cannot tell you the
conclusion is right — for that you need constructed ground truth (seed a known shift
with append_monthly_data.py, assert the agent finds it), which mutates the CSVs and
needs a full dbt run. Worth doing; not the first version.

Being honest about that matters, because a green snapshot run is easy to read as
"the diagnoses are correct" when it only means "they are unchanged".

What gets snapshotted
---------------------
Structural conclusions, not prose. Shares are rounded to whole percents and totals
are dropped entirely, because the point is "did the CONCLUSION move", and a snapshot
that fails on float drift gets ignored within a week. So this catches a dimension
that stopped explaining, a verdict that flipped, a warning that vanished, a probe
that started failing — and deliberately ignores a total moving by a cent.

Usage
-----
    cd gateway
    python audit_diagnoses.py                  # compare against the committed baseline
    python audit_diagnoses.py --update         # accept current output as the baseline
    python audit_diagnoses.py --scenario churn-june-2026
    python audit_diagnoses.py --show           # print each answer in full

Exits 1 on any drift, so it works as a CI gate. Needs the DuckDB warehouse; without
it the run reports why and exits 0 rather than failing a build for a missing 300 MB
artifact.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

DEFAULT_SNAPSHOT = os.path.join(_HERE, "diagnosis_snapshots.json")


@dataclass(frozen=True)
class Scenario:
    """One canned diagnosis. Each exists to pin a specific behaviour."""

    id: str
    metric: str
    start: str
    end: str
    why: str
    filters: tuple[tuple[str, str], ...] = ()
    max_dimensions: int = 3
    # Resolve the window from the warehouse instead of using start/end. A scenario
    # that pins a MOVING property of the data must not hardcode a date: the spine in
    # fct_mrr_monthly runs to current_date(), so "the trailing churn-only period" was
    # 2026-09 when this file was written and is 2026-10 now. The hardcoded version
    # kept passing while quietly testing an ordinary month with 12,032 active
    # subscribers in it. Same failure as the dashboard's old wall-clock anchor.
    # `target` is in the snapshot, so the window moving is visible in the diff.
    resolve: str | None = None


# Chosen to cover the behaviours most likely to move, one per interesting code path.
# Add a scenario when you add a behaviour; that is cheaper than discovering the gap
# from a user.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="churn-june-2026",
        metric="churn_rate", start="2026-06-01", end="2026-06-30",
        why="single-month target against an unrepresentative baseline; the case that "
            "produced +163.7% against a May 39% below trend",
    ),
    Scenario(
        id="churn-h1-2026",
        metric="churn_rate", start="2026-01-01", end="2026-06-30",
        why="multi-month target, so the baseline check must stay silent; also the "
            "weighted mix-vs-rate split via monthly_subscriber_base",
    ),
    Scenario(
        id="revenue-h1-2026",
        metric="total_revenue", start="2026-01-01", end="2026-06-30",
        why="additive decomposition with no weight_metric; the broad-based verdict",
    ),
    Scenario(
        id="revenue-germany",
        metric="total_revenue", start="2026-01-01", end="2026-06-30",
        filters=(("country", "DE"),),
        why="a filter pinning a dimension must drop it as an axis, and must scope "
            "the comparison side too",
    ),
    Scenario(
        id="payment-failure-h1-2026",
        metric="payment_failure_rate", start="2026-01-01", end="2026-06-30",
        why="decompose_first must put failure_reason ahead of the global order",
    ),
    Scenario(
        id="churn-phantom-period",
        metric="churn_rate", start="2026-09-01", end="2026-09-30",
        resolve="phantom_period",
        why="the trailing churn-only period must raise a high-severity data warning",
    ),
    Scenario(
        id="mrr-h1-2026",
        metric="mrr", start="2026-01-01", end="2026-06-30",
        why="mrr_type is the MRR bridge; pins that it stays a first-class axis",
    ),
    Scenario(
        id="ltv-h1-2026",
        metric="ltv", start="2026-01-01", end="2026-06-30",
        why="cross-model ratio - only dim_subscribers dimensions are groupable",
    ),
    Scenario(
        id="revenue-year-over-year",
        metric="total_revenue", start="2025-01-01", end="2025-12-31",
        why="a completed past year: 12 whole months, no trim, real attribution. "
            "NOTE it cannot by itself distinguish year-over-year from the preceding "
            "period, because for a full 12-month window the two are the SAME window "
            "- that is `revenue-current-year` below",
    ),
    Scenario(
        id="revenue-current-year",
        metric="total_revenue", start="", end="", resolve="current_year",
        why="THE case this exists for: a year still in progress must be trimmed to "
            "complete months and compared against the SAME months a year earlier. "
            "Resolved rather than hardcoded, following churn-phantom-period: the "
            "window moves every month, and `target` is in the snapshot so it shows "
            "as a diff line instead of silently testing nothing. This is the only "
            "scenario where year-over-year and the preceding period differ",
    ),
    Scenario(
        id="engagement-h1-2026",
        metric="engagement_rate", start="2026-01-01", end="2026-06-30",
        why="session metric weighted by total_sessions; content_primary_genre must "
            "stay out as an all-null axis",
    ),
)


@dataclass
class Services:
    validator: object
    sql_generator: object
    registry: object
    app: object = field(default=None)
    pool: object = field(default=None)


def _build_services():
    """Stand up the real services against the real warehouse, or explain why not."""
    from config import settings

    settings.diagnostics_enabled = True

    from core.duckdb_pool import DuckDBPool, resolve_duckdb_path
    from core.manifest_parser import ManifestParser
    from core.metric_registry import MetricRegistry
    from core.semantic_validator import SemanticValidator
    from core.sql_generator import SQLGenerator
    from core.sql_template_cache import SQLTemplateCache

    os.environ["DUCKDB_PATH"] = resolve_duckdb_path(settings.duckdb_path)
    if not os.path.exists(os.environ["DUCKDB_PATH"]):
        return None, (
            f"warehouse not found at {os.environ['DUCKDB_PATH']}. Rebuild it with "
            "`python load_raw_data_to_duckdb.py` then `dbt run`."
        )

    parser = ManifestParser()
    parser.load(settings.manifest_path)
    registry = MetricRegistry()
    registry.load(settings.metrics_path, settings.semantic_models_path, parser)

    pool = DuckDBPool(settings=settings)
    try:
        pool.initialise()
    except Exception as exc:
        return None, f"could not open the warehouse ({exc}). Is the gateway running?"

    cache = SQLTemplateCache(
        ttl_seconds=settings.sql_template_cache_ttl_seconds,
        maxsize=settings.sql_template_cache_maxsize,
        # `disk_path=None` DELIBERATELY, and it is not the bug that was fixed here.
        # This used to be `SQLTemplateCache(ttl, path)`, which bound the path to
        # `maxsize` and left disk_path None by accident; the int is the fix. Passing
        # the real path would be a different mistake: `set()` always `_save()`s and
        # the cache has no read-only mode, so every run of this tool would rewrite
        # `.sql_template_cache.json` -- a committed build artifact owned by
        # `precompile_templates.py` -- and the file would end up depending on
        # whichever tool ran last. An audit must not mutate what it measures.
        #
        # Nothing is lost: MetricFlow compiles on the happy path and L1 is only a
        # fallback, so an empty cache exercises the real compile path rather than
        # masking an engine failure behind 69 committed templates.
        disk_path=None,
    )
    services = Services(
        validator=SemanticValidator(registry),
        pool=pool,
        sql_generator=SQLGenerator(settings=settings, pool=pool, template_cache=cache),
        registry=registry,
    )
    services.app = types.SimpleNamespace(state=types.SimpleNamespace(
        semantic_validator=services.validator,
        sql_generator=services.sql_generator,
        metric_registry=registry,
        query_cache=None,
        dimension_values={},
    ))
    return services, ""


def _pct(value: float | None) -> str | None:
    """Whole percents. Float drift must not fail a snapshot or nobody reads it."""
    return None if value is None else f"{value * 100:.0f}%"


def _resolve_window(scenario: Scenario, services: Services) -> tuple[str, str]:
    """Resolve a scenario whose window tracks a moving property of the data."""
    if scenario.resolve is None:
        return scenario.start, scenario.end
    if scenario.resolve == "current_year":
        # The year in progress, as the LLM emits it for "this fiscal year". The
        # planner trims it to complete months, so what this pins is that the trim
        # and the year-over-year comparison both happen.
        year = date.today().year
        return f"{year}-01-01", f"{year}-12-31"

    if scenario.resolve != "phantom_period":
        raise ValueError(f"unknown resolve strategy: {scenario.resolve!r}")

    # The phantom period is the LAST spine month, which carries the +1 month
    # cancellation offset and so has churn rows and zero active subscribers.
    rows = services.pool.execute(
        "SELECT MAX(period_month) AS m FROM marts.fct_mrr_monthly"
    )
    month = rows[0]["M"]
    start = month.strftime("%Y-%m-%d")
    # MetricFlow widens a monthly-grain range to whole months anyway, so naming the
    # first day for both ends is exact and avoids month-length arithmetic.
    return start, start


def run_scenario(scenario: Scenario, services: Services) -> dict:
    """Run one diagnosis and reduce it to its structural conclusions."""
    import api.routes.query as route
    from core.diagnostics.artifacts import ArtifactRegistry
    from core.intent_extractor import FilterClause, TimeRange

    start, end = _resolve_window(scenario, services)

    class _Options:
        include_sql = False
        max_rows = 100

    body = types.SimpleNamespace(
        query=f"why did {scenario.metric} change", options=_Options()
    )
    intent = types.SimpleNamespace(
        metrics=[scenario.metric],
        dimensions=[],
        filters=[FilterClause(column=c, operator="eq", value=v)
                 for c, v in scenario.filters],
        time_range=TimeRange(start_date=start, end_date=end),
        query_type="diagnostic_query",
    )

    started = time.perf_counter()
    result = route._run_diagnosis(
        body, intent, types.SimpleNamespace(app=services.app), scenario.id
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    if result is None:
        return {"id": scenario.id, "outcome": "fell_through", "answer": ""}

    answer = result["answer"]
    # The verdict is read off the answer rather than recomputed, because the answer is
    # what a user sees - if the wording and the structure ever disagree, the wording
    # is the bug.
    broad_based = "broad-based rather than concentrated" in answer
    baseline_flagged = "not a typical baseline" in answer

    snapshot = {
        "id": scenario.id,
        "outcome": "ok",
        "metric": result["metric"],
        "target": result["target_window"],
        "comparison": result["comparison_window"],
        "dimensions": result["dimensions_examined"],
        "probes_planned": len(result["evidence"]),
        "probes_failed": sum(1 for e in result["evidence"] if e["error"]),
        "verdict": "broad_based" if broad_based else "concentrated_or_none",
        "baseline_flagged": baseline_flagged,
        "hypotheses": [
            {
                "dimension": h["dimension"],
                "verdict": h["verdict"],
                "confidence": h["confidence"],
                "explained": _pct(h["explained_share"]),
            }
            for h in result["hypotheses"]
        ],
        "data_warnings": sorted(w["id"] for w in result.get("data_warnings") or []),
        "caution_count": len(result["cautions"]),
        "note_count": len(result["notes"]),
        "answer_lines": len(answer.splitlines()),
    }
    return snapshot, answer, elapsed_ms


def _load_baseline(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return {s["id"]: s for s in (json.load(fh).get("scenarios") or [])}


def _write_baseline(path: str, snapshots: list[dict]) -> None:
    payload = {
        "note": (
            "Structural conclusions of canned diagnoses. Regenerate with "
            "`python audit_diagnoses.py --update` and review the diff - a change "
            "here means an answer changed."
        ),
        "scenarios": sorted(snapshots, key=lambda s: s["id"]),
    }
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _diff(before: dict, after: dict) -> list[str]:
    """Field-level differences, one line each. Empty when nothing moved."""
    if not before:
        return ["NEW scenario (no baseline)"]
    changes: list[str] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, "<absent>"), after.get(key, "<absent>")
        if old != new:
            changes.append(f"{key}: {old!r} -> {new!r}")
    return changes


def main() -> int:
    ap = argparse.ArgumentParser(description="Snapshot diagnostic conclusions.")
    ap.add_argument("--update", action="store_true",
                    help="accept the current output as the baseline")
    ap.add_argument("--scenario", action="append", default=None,
                    help="limit to these scenario ids (repeatable)")
    ap.add_argument("--show", action="store_true", help="print each answer in full")
    ap.add_argument("--out", default=DEFAULT_SNAPSHOT)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )

    scenarios = SCENARIOS
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = tuple(s for s in SCENARIOS if s.id in wanted)
        missing = wanted - {s.id for s in scenarios}
        if missing:
            sys.exit(f"unknown scenario(s): {sorted(missing)}")

    print("Diagnosis snapshot audit")
    print(f"  {len(scenarios)} scenario(s)")

    services, why = _build_services()
    if services is None:
        # Exit 0: a missing 300 MB gitignored artifact is not a code regression.
        print(f"  SKIPPED - {why}")
        return 0

    print("  building the warm MetricFlow engine on the first probe (~40s cold)...\n")

    baseline = _load_baseline(args.out)
    snapshots: list[dict] = []
    drifted: list[tuple[str, list[str]]] = []

    for scenario in scenarios:
        outcome = run_scenario(scenario, services)
        if isinstance(outcome, dict):          # fell through
            snapshot, answer, elapsed = outcome, "", 0.0
        else:
            snapshot, answer, elapsed = outcome
        snapshots.append(snapshot)

        changes = _diff(baseline.get(scenario.id, {}), snapshot)
        marker = "DRIFT" if changes else "same "
        if changes and baseline.get(scenario.id):
            drifted.append((scenario.id, changes))
        print(f"  [{marker}] {scenario.id:26s} {snapshot['outcome']:12s} "
              f"{elapsed:6.0f} ms  {snapshot.get('verdict', '-')}")
        if changes:
            for line in changes:
                print(f"            {line}")
        if args.show and answer:
            for line in answer.splitlines():
                print(f"            | {line}")

    print()
    if args.update:
        # Merge, so a narrowed run does not silently delete other scenarios'
        # baselines - that would read as "no drift" on the next full run.
        merged = {**baseline, **{s["id"]: s for s in snapshots}}
        _write_baseline(args.out, list(merged.values()))
        print(f"Baseline updated: {args.out} ({len(merged)} scenario(s))")
        print("Review the diff before committing - it records what the agent concludes.")
        return 0

    if not baseline:
        print("No baseline yet. Run with --update to create one.")
        return 0

    if drifted:
        print(f"{len(drifted)} scenario(s) changed their conclusions:")
        for scenario_id, changes in drifted:
            print(f"  {scenario_id}: {len(changes)} field(s)")
        print("\nIf the change is intended, re-run with --update and commit the diff.")
        print("This tool records what the agent concludes; it does not judge whether "
              "the conclusion is correct.")
        return 1

    print(f"No drift across {len(scenarios)} scenario(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
