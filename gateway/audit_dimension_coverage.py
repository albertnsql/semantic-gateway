"""
audit_dimension_coverage.py — verify every certified metric x dimension pair
actually compiles through MetricFlow.

Why this exists
---------------
``MetricRegistry.certified_dimensions`` is an *assertion*, not a proof. It is
built from the owning semantic model, then extended by a hand-maintained second
pass in ``metric_registry.load()`` for metrics that reach ``dim_subscribers``
through a foreign entity. That second pass is correct in principle — sem_mrr and
sem_payments both declare ``subscriber`` as a foreign entity, so MetricFlow
compiles the join unaided — but it extends with
``sub_sem.dimensions + sub_sem.time_dimensions`` wholesale. Nobody has checked
that every resulting pair resolves.

The route already trusts these pairs: ``query.py``'s Stage 1.5 rejects a query
whose dimension is not certified, and accepts one that is. So an over-broad
claim does not fail loudly — it fails at compile time, one layer further down,
after the user has already been told the question was valid.

That is survivable for the single-query path (one rejected query) and not
survivable for a diagnostic agent, which picks its probes from this same list.
An uncompilable pair there costs a probe slot mid-run and the agent reaches its
conclusion with a hole in the evidence it does not know about.

So: push all of them through the real compiler and write down which ones work.

What it tests
-------------
Three axes, because there are three ways a pair can be unusable and the registry
only ever claimed the first:

1. **Compiles** — can MetricFlow resolve this metric grouped by this dimension.
2. **Populated** — does the dimension actually carry data. On by default; pass
   ``--no-execute`` to skip.
3. **Attributed** — does the MEASURE actually land in those buckets, or drain
   into a NULL / 'unknown' one. Runs with the population check.

The third exists because the second is not enough either. A dimension can have
fifteen healthy country buckets and still leave a quarter of revenue
unattributed: during the June-2026 orphan append, 16,602 payment rows lost their
country to a LEFT JOIN that no longer matched, putting **24.6% of 2026 revenue**
($336,095 of $1,366,490) in an Unknown bucket — and ``population`` scored that
pair ``ok``, because the fifteen real buckets were still there. A breakdown like
that is not empty, which is the problem: it looks like an answer.

It is measured only where the arithmetic holds. ``sum`` and ``count`` measures
are summable across buckets; ``average`` is not (a mean of means needs weights),
and ``count_distinct`` is not (an entity in two buckets inflates a bucket-sum
denominator, which UNDERSTATES the unattributed share — the dangerous direction).
Ratio and derived metrics return only the ratio, so the volume behind it is not
in the result set at all. Those are reported ``n/a`` rather than guessed at; see
``_additive_metrics()``. 11 of 23 metrics qualify, including ``total_revenue``,
which is both the motivating case and ``ltv``'s numerator.

'unknown' counts as unattributed alongside NULL, and that is not cosmetic:
``fct_stream_sessions`` deliberately coalesces a missing genre to ``'unknown'``,
so a NULL-only check reports 0.00% for ``total_sessions x
content_primary_genre`` while the true figure is 0.45%. The coalesce would have
hidden precisely what this axis looks for.

A high share is frequently CORRECT — ``mrr x churn_reason`` is 80.7%
unattributed because most MRR belongs to active subscribers who have no churn
reason — so ``--fail-on-new`` keys on the verdict *changing*, never on the level.
A by-construction null is stable; a broken join is a change. Failing on the level
would report 19 permanent "problems" and be ignored within a week.

The second exists because the first is not enough, and a live query proved it:
"average engagement by content type over the last 3 months" compiled, executed,
and returned a single row with genre = ``null`` and engagement 63.563, which the
narrative then described as "a uniform engagement level across the platform's
content library". ``fct_stream_sessions.content_primary_genre`` is 100% null —
``dim_content`` sources it from a bridge table sharing zero content_ids with its
own spine. Compiling forever, useless forever.

Reachability is probed with no time constraint, no ``--where`` and no ``--limit``,
so a pair that passes is not thereby proven correct for any *particular* query
(time-grain validity is a separate axis, already covered by
``get_valid_time_grains_for_metric``).

Normalisations are applied first, in the same order ``SQLGenerator.generate()``
applies them, because they change what MetricFlow is actually asked:

* bare name -> qualified name via ``build_dimension_prefix_map()``
* ``correct_dimension_entity()`` — rewrites an unreachable entity prefix
* ``require_metric_time()`` — prepends ``metric_time__<grain>`` for offset metrics

Skipping them would report false failures for exactly the cases those helpers
were written to fix (``net_mrr_growth`` cannot resolve without a time grain).

Cost
----
One engine build (20-105 s, cold imports dominate) plus ~20-60 ms per pair, so
the matrix itself is seconds and the build is the whole cost. Compile-only: no rows are read, nothing is written to the warehouse.
Stop the gateway first — DuckDB's writer lock is process-exclusive.

Usage
-----
    cd gateway
    python audit_dimension_coverage.py                     # compile + population + attribution
    python audit_dimension_coverage.py --metric total_revenue --metric mrr
    python audit_dimension_coverage.py --no-execute        # compile only, no warehouse
    python audit_dimension_coverage.py --fail-on-new       # CI gate; see below
    python audit_dimension_coverage.py --out coverage.csv

``--fail-on-new`` exits 1 when a pair that used to compile no longer does, OR when
one that used to carry data now returns an empty dimension. The second half is the
quieter failure: it still compiles, still returns a row, and only the answer is
empty.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# The gateway's path settings are written relative to gateway/ (its cwd in
# production). Resolve from this file's directory so the script behaves the same
# from any working directory — the same reasoning as resolve_duckdb_path().
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

DEFAULT_OUT = os.path.join(_HERE, "dimension_coverage.csv")

# MetricFlow answers every unresolvable group-by with the SAME message — "the
# given input does not match any of the available group-by-items" — followed by a
# blurb listing all three common causes. So the text cannot tell you which cause
# applied, and an earlier version of this script classified on that blurb and
# reported `needs_metric_time` for failures that had nothing to do with time.
#
# Classify structurally instead. Both checks below are computed from the semantic
# layer, not parsed from a string, so they are right by construction.
_MSG_UNRESOLVED = "does not match any of the available group-by-items"


@dataclass
class PairResult:
    """One metric x dimension probe."""

    metric: str
    bare: str
    probed: str
    group_by: list[str]
    ok: bool
    reason: str = ""
    detail: str = ""
    ms: float = 0.0
    sql_chars: int = 0
    # Qualifying a bare name via the prefix map happens to essentially every pair
    # and is not interesting. These two are: an entity prefix that was WRONG and
    # had to be rewritten, and a metric that cannot resolve without a time grain.
    entity_corrected: bool = False
    time_injected: bool = False
    # ── Population: does the pair actually return DATA, not just compile ──────
    # "" when execution was skipped; otherwise one of _POP_*.
    population: str = ""
    rows: int | None = None
    dim_distinct: int | None = None
    # ── Attribution: what SHARE of the measure lands in an unattributed bucket ─
    # Distinct from `population`, which only asks whether real buckets exist. A
    # dimension can have 15 healthy countries AND still leave a quarter of revenue
    # in a NULL bucket — that is what the June-2026 orphan append did ($336,095,
    # 24.6% of 2026 revenue), and `population` scored it `ok`.
    # "" when not computed; otherwise one of _ATTR_*.
    attribution: str = ""
    unattributed_pct: float | None = None


# A compiling pair can still be useless. Verdicts, in order of severity.
_POP_OK = "ok"              # >= 2 distinct non-null dimension values
_POP_SINGLE = "single_value"  # exactly 1 — suspicious, sometimes legitimate
_POP_ALL_NULL = "all_null"   # 0 — the dimension is empty; a breakdown is impossible
_POP_ZERO_ROWS = "zero_rows"  # the query returned nothing at all
_POP_ERROR = "exec_error"    # compiled but would not run

# all_null is the one that silently produces a confident wrong answer, so it is
# the only verdict treated as a hard failure by --fail-on-new.
_POP_FAILURES = frozenset({_POP_ALL_NULL, _POP_ERROR})

# ── Attribution verdicts ────────────────────────────────────────────────────
_ATTR_OK = "attributed"        # unattributed share below the threshold
_ATTR_SKEWED = "unattributed"  # at or above it — a breakdown here hides real volume
_ATTR_NA = "n/a"               # not computable; see _additive_metrics()

# 5% is a judgement, not a discovered constant. A dimension sourced from a clean
# join should sit near zero, and the case this check exists for was 24.6%. Set it
# lower and `churn_reason` (legitimately null for non-churned subscribers) would
# dominate the report; higher and a fifth of revenue could go missing quietly.
_ATTR_THRESHOLD_PCT = 5.0

# Values that mean "we do not know", not "this is a category". `fct_stream_sessions`
# deliberately coalesces a missing genre to 'unknown', so a NULL-only check would
# report 0.00% for the one dimension we KNOW is 0.45% unattributed — the coalesce
# would hide exactly what this check is looking for. Kept deliberately short: a
# longer list starts swallowing legitimate categories.
_UNATTRIBUTED_VALUES = frozenset({"unknown"})


@dataclass
class Summary:
    results: list[PairResult] = field(default_factory=list)

    @property
    def passed(self) -> list[PairResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[PairResult]:
        return [r for r in self.results if not r.ok]

    @property
    def unpopulated(self) -> list[PairResult]:
        return [r for r in self.results if r.population in _POP_FAILURES]


def _classify(message: str, metric: str, probed: str, metric_type: str) -> str:
    """
    Name the cause of a failed probe from the semantic layer, not the message.

    Three outcomes, in order of how actionable they are:

    ``unreachable_entity_prefix``
        The prefix names an entity the metric cannot reach at all, so
        ``build_dimension_prefix_map()`` handed out a name that could never
        resolve. A map defect, and the most serious kind — the same map is what
        ``correct_dimension_entity()`` consults for a replacement, so the guard
        finds the identical bad value and passes it through.

    ``ratio_narrower_than_claimed``
        The prefix is reachable but the metric is a ratio, and a ratio can only be
        grouped by dimensions reachable from **every** input. ``certified_dimensions``
        is built from one semantic model, so for a cross-model ratio it claims a
        union where MetricFlow requires an intersection.

    ``unresolved`` / ``other``
        Genuinely unexplained; read ``detail``.
    """
    from core.sql_generator import metric_entities

    if "__" in probed:
        entity = probed.split("__", 1)[0]
        if entity != "metric_time":
            reachable = metric_entities(metric)
            if reachable and entity not in reachable:
                return "unreachable_entity_prefix"

    if metric_type == "ratio":
        return "ratio_narrower_than_claimed"

    if _MSG_UNRESOLVED in (message or "").lower():
        return "unresolved"
    return "other"


def _load_registry():
    """Load the live registry exactly as the route does. Returns (registry, settings)."""
    from config import settings
    from core.manifest_parser import ManifestParser
    from core.metric_registry import MetricRegistry

    parser = ManifestParser()
    parser.load(settings.manifest_path)
    registry = MetricRegistry()
    registry.load(settings.metrics_path, settings.semantic_models_path, parser)
    return registry, settings


def _build_engine(settings):
    """
    Build the warm MetricFlow engine, or exit with a message naming the fix.

    Unlike the gateway — where a failed build silently degrades to the `mf`
    subprocess — there is no point continuing here. A subprocess audit would cost
    ~30 s per pair, so 242 pairs would take two hours.
    """
    from core.duckdb_pool import resolve_duckdb_path
    from core.metricflow_engine import WarmMetricFlowEngine

    # dbt-duckdb resolves a relative path against the CWD, not the project dir,
    # so MetricFlow would otherwise compile against a different (or absent) file.
    # main.py does exactly this before building the engine.
    os.environ["DUCKDB_PATH"] = resolve_duckdb_path(settings.duckdb_path)

    project_dir = getattr(settings, "dbt_project_dir", "")
    if not project_dir:
        sys.exit("dbt_project_dir is unset in config - cannot build the engine.")

    started = time.perf_counter()
    engine = WarmMetricFlowEngine.try_build(
        dbt_project_dir=project_dir, dbt_profiles_dir=project_dir
    )
    if engine is None:
        sys.exit(
            "Could not build the in-process MetricFlow engine.\n"
            f"  dbt project : {os.path.abspath(project_dir)}\n"
            f"  DUCKDB_PATH : {os.environ['DUCKDB_PATH']}\n"
            "Check that target/semantic_manifest.json exists (run `dbt parse`), and "
            "that no other process holds the DuckDB write lock."
        )
    print(f"  engine ready in {time.perf_counter() - started:.1f}s")
    return engine


def _open_warehouse(settings):
    """
    Open a connection for the population check, or return None with a reason.

    It has to be READ-WRITE, which looks wrong and is not. DuckDB refuses a second
    connection to the same file with a different configuration, dbt-duckdb hardcodes
    `read_only=False`, and this process hosts the warm MetricFlow engine — which
    goes through dbt-duckdb. Verified: `duckdb.connect(path, read_only=True)` after
    the engine is built raises "Can't open a connection to same database file with a
    different configuration". This is the same corner `DuckDBPool` is in, and the
    same answer: connect read-write and enforce read-only in SQL via
    assert_read_only(). See CLAUDE.md, DuckDB section, point 3.
    """
    path = os.environ.get("DUCKDB_PATH", "")
    if not path or not os.path.exists(path):
        return None, f"warehouse file not found at {path or '(DUCKDB_PATH unset)'}"
    try:
        import duckdb

        return duckdb.connect(path), ""
    except Exception as exc:  # a locked file, a bad build, anything
        return None, f"{type(exc).__name__}: {exc}"


def _additive_metrics(manifest_path: str) -> dict[str, str]:
    """metric -> aggregation, for metrics whose measure can be SUMMED across buckets.

    Only `sum` and `count` qualify, and the exclusions are the whole point:

    * `average` (avg_watch_time, engagement_rate, avg_buffering_events) — the mean
      of a set of means is not the overall mean without weights, so a bucket-sum
      denominator is arithmetically wrong, not merely imprecise.
    * `count_distinct` (total_subscribers, monthly_subscriber_base) — an entity
      appearing in two buckets is counted twice by a bucket-sum, which INFLATES the
      denominator and therefore UNDERSTATES the unattributed share. Understating is
      the dangerous direction for a safety check, so it is skipped rather than
      approximated.
    * `ratio` / `derived` (churn_rate, ltv, recommendation_ctr, retention_rate,
      net_mrr_growth, payment_failure_rate) — MetricFlow returns only the ratio, so
      the volume behind it is not in the result set at all. Getting these right
      needs a second probe for the denominator; deliberately not done here.

    `total_revenue` IS additive, which matters: it is the metric whose null-country
    bucket motivated this check, and `ltv`'s numerator, so the signal is reachable
    for the case that prompted it.

    Two-hop resolution (metric -> measure -> agg) rather than name matching, for the
    same reason `test_metric_time_columns_match_the_semantic_layer` does it: metric
    and measure names diverge (`total_subscribers` uses `active_subscribers_count`).
    """
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except Exception:
        return {}

    aggs = {
        measure["name"]: measure.get("agg")
        for model in manifest.get("semantic_models", [])
        for measure in model.get("measures", [])
        if measure.get("name")
    }

    out: dict[str, str] = {}
    for metric in manifest.get("metrics", []):
        if metric.get("type") != "simple":
            continue
        measure = (metric.get("type_params") or {}).get("measure") or {}
        name = measure.get("name") if isinstance(measure, dict) else measure
        agg = aggs.get(name)
        if agg in ("sum", "count"):
            out[metric["name"]] = agg
    return out


def _check_attribution(
    columns: list[str], rows: list, dim_idx: int, metric: str, agg: str | None
) -> tuple[str, float | None]:
    """Share of the measure sitting in an unattributed bucket.

    Returns (verdict, percent). `_ATTR_NA` with None when the measure cannot be
    summed across buckets — see _additive_metrics(). Inventing a number for an
    average would be worse than reporting nothing.

    The measure column is found BY NAME, like the dimension column, and for the
    same reason: an offset metric gets `metric_time__month` prepended, so position
    is not reliable.
    """
    if not agg:
        return _ATTR_NA, None

    try:
        measure_idx = columns.index(metric)
    except ValueError:
        # MetricFlow normally names the column after the metric. If it did not,
        # say so rather than summing whichever column looks numeric.
        return _ATTR_NA, None

    total = 0.0
    unattributed = 0.0
    for row in rows:
        value = row[measure_idx]
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            return _ATTR_NA, None
        total += value
        label = row[dim_idx]
        if label is None or (
            isinstance(label, str) and label.strip().lower() in _UNATTRIBUTED_VALUES
        ):
            unattributed += value

    if total <= 0:
        # No volume at all: a percentage would be a division artefact, and
        # `population` already reports the emptiness.
        return _ATTR_NA, None

    pct = 100.0 * unattributed / total
    verdict = _ATTR_SKEWED if pct >= _ATTR_THRESHOLD_PCT else _ATTR_OK
    return verdict, pct


def _check_population(
    con, sql: str, probed: str, metric: str = "", agg: str | None = None
) -> tuple[str, int | None, int | None, str, float | None]:
    """
    Execute *sql* and judge whether the dimension actually carries data.

    Returns (verdict, row_count, distinct_non_null_dimension_values,
    attribution_verdict, unattributed_percent).

    The result set is fetched ONCE and both checks read it. They answer different
    questions — population asks "are there real buckets", attribution asks "does
    the volume actually land in them" — and a dimension can pass the first and fail
    the second, which is the case this was extended for.

    The signal is the number of distinct non-null dimension values, NOT a row-level
    null percentage — the compiled query is already aggregated, so it returns one
    row per dimension value and a row-level rate would be meaningless. Observed:
    `churned_subscribers x subscriber__churn_reason` returns 7 rows of which one is
    a `(None, 0)` bucket. That is healthy, and a naive null rate would score it 14%
    broken. `engagement_rate x session__content_primary_genre` returns exactly one
    row, `(None, 58.29)` — zero real buckets, which is the shape worth failing on.

    The dimension column is located BY NAME. MetricFlow names result columns after
    the qualified dimension, and it cannot be assumed to be first: an offset metric
    gets `metric_time__month` prepended, so `net_mrr_growth x subscription__plan_type`
    returns three columns with the dimension in the middle.
    """
    from core.duckdb_pool import assert_read_only

    # MetricFlow only emits SELECT/WITH, but the connection is read-write, so this
    # is the same backstop the serving path uses rather than a trust exercise.
    assert_read_only(sql)

    cursor = con.execute(sql)
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    if not rows:
        return _POP_ZERO_ROWS, 0, 0, _ATTR_NA, None

    try:
        idx = columns.index(probed)
    except ValueError:
        # Should not happen — but guessing a column would silently judge the wrong
        # one, so say so instead.
        return _POP_ERROR, len(rows), None, _ATTR_NA, None

    attribution, pct = _check_attribution(columns, rows, idx, metric, agg)

    distinct_non_null = len({r[idx] for r in rows if r[idx] is not None})
    if distinct_non_null == 0:
        return _POP_ALL_NULL, len(rows), 0, attribution, pct
    if distinct_non_null == 1:
        return _POP_SINGLE, len(rows), 1, attribution, pct
    return _POP_OK, len(rows), distinct_non_null, attribution, pct


def _probe(engine, metric: str, bare: str, prefix_map: dict,
           metric_type: str = "", con=None, additive: dict | None = None) -> PairResult:
    """Normalise one pair the way the route would, then compile it."""
    from core.sql_generator import correct_dimension_entity, require_metric_time

    qualified = (prefix_map.get(metric) or {}).get(bare, bare)
    probed = correct_dimension_entity(qualified, metric)
    group_by = require_metric_time(metric, [probed])

    flags = {
        "entity_corrected": probed != qualified,
        "time_injected": len(group_by) > 1,
    }

    argv = ["mf", "query", "--explain", "--metrics", metric,
            "--group-by", ",".join(group_by)]

    started = time.perf_counter()
    try:
        sql = engine.explain_argv(argv)
    except Exception as exc:  # MetricFlow raises a wide variety; all mean "no"
        message = f"{type(exc).__name__}: {exc}"
        return PairResult(
            metric=metric, bare=bare, probed=probed, group_by=group_by, ok=False,
            reason=_classify(message, metric, probed, metric_type),
            detail=message[:400].replace("\n", " "),
            ms=(time.perf_counter() - started) * 1000, **flags,
        )

    result = PairResult(
        metric=metric, bare=bare, probed=probed, group_by=group_by, ok=True,
        ms=(time.perf_counter() - started) * 1000, sql_chars=len(sql or ""), **flags,
    )

    # Compiling is not the same as being populated. Only run for pairs that
    # compiled — there is nothing to execute otherwise.
    if con is not None:
        try:
            (
                result.population, result.rows, result.dim_distinct,
                result.attribution, result.unattributed_pct,
            ) = _check_population(
                con, sql, probed, metric, (additive or {}).get(metric)
            )
        except Exception as exc:
            result.population = _POP_ERROR
            result.detail = f"{type(exc).__name__}: {exc}"[:400].replace("\n", " ")

    return result


def _read_previous(path: str) -> dict[tuple[str, str], tuple[bool, str, str]]:
    """
    Load a prior run's verdicts, keyed (metric, bare) -> (compiled, population).

    `population` is "" for a baseline written before the column existed, or by a
    --no-execute run. The diff treats that as "unknown" rather than as a change,
    so an older baseline degrades to compile-only comparison instead of reporting
    238 phantom regressions.
    """
    if not os.path.exists(path):
        return {}
    previous: dict[tuple[str, str], tuple[bool, str, str]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            previous[(row["metric"], row["bare_dimension"])] = (
                row["ok"] == "true",
                row.get("population", "") or "",
                row.get("attribution", "") or "",
            )
    return previous


def _write_csv(path: str, results: list[PairResult]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "metric", "bare_dimension", "probed_dimension", "group_by",
            "ok", "reason", "population", "rows", "dim_distinct",
            "attribution", "unattributed_pct",
            "entity_corrected", "time_injected",
            "ms", "sql_chars", "detail",
        ])
        for r in sorted(results, key=lambda x: (x.metric, x.bare)):
            writer.writerow([
                r.metric, r.bare, r.probed, ",".join(r.group_by),
                "true" if r.ok else "false", r.reason,
                r.population,
                "" if r.rows is None else r.rows,
                "" if r.dim_distinct is None else r.dim_distinct,
                r.attribution,
                "" if r.unattributed_pct is None else f"{r.unattributed_pct:.2f}",
                "true" if r.entity_corrected else "false",
                "true" if r.time_injected else "false",
                f"{r.ms:.1f}", r.sql_chars, r.detail,
            ])


def _report(summary: Summary, previous: dict[tuple[str, str], tuple[bool, str, str]]) -> int:
    """Print the human-readable summary. Returns the count of regressions."""
    results = summary.results
    executed = any(r.population for r in results)
    by_metric: dict[str, list[PairResult]] = defaultdict(list)
    for r in results:
        by_metric[r.metric].append(r)

    print(f"\n{'metric':26s} {'pass':>6s} {'fail':>6s} {'empty':>6s}  problem dimensions")
    print("-" * 100)
    for metric in sorted(by_metric):
        rows = by_metric[metric]
        bad = [r for r in rows if not r.ok]
        empty = [r for r in rows if r.population in _POP_FAILURES]
        names = ", ".join(sorted(r.bare for r in bad + empty))
        if len(names) > 44:
            names = names[:41] + "..."
        print(f"{metric:26s} {len(rows) - len(bad):6d} {len(bad):6d} "
              f"{len(empty):6d}  {names}")

    print("-" * 100)
    total = len(results)
    ok = len(summary.passed)
    pct = (ok / total * 100) if total else 0.0
    print(f"{'TOTAL':26s} {ok:6d} {total - ok:6d} "
          f"{len(summary.unpopulated):6d}  {pct:.1f}% of {total} pairs compile")

    if executed:
        counts = Counter(r.population for r in results if r.population)
        print("\nPopulation (does the dimension actually carry data)")
        for verdict in (_POP_OK, _POP_SINGLE, _POP_ZERO_ROWS, _POP_ALL_NULL, _POP_ERROR):
            if counts.get(verdict):
                print(f"  {counts[verdict]:4d}  {verdict}")
        for r in summary.unpopulated:
            print(f"  [EMPTY] {r.metric} x {r.bare} -> {r.population} "
                  f"(rows={r.rows}, distinct={r.dim_distinct})")
        singles = [r for r in results if r.population == _POP_SINGLE]
        if singles:
            print("  single-value dimensions (may be legitimate, worth a look):")
            for r in singles:
                print(f"      {r.metric} x {r.bare}")
    else:
        print("\nPopulation not checked - compile-only run. A pair can compile and "
              "still return an empty dimension.")

    if summary.failed:
        print("\nFailures by cause")
        for reason, count in Counter(r.reason for r in summary.failed).most_common():
            print(f"  {count:4d}  {reason}")
        worst = sorted(summary.failed, key=lambda r: r.reason)[:3]
        print("\nSample messages")
        for r in worst:
            print(f"  {r.metric} x {r.bare} [{r.reason}]")
            print(f"      {r.detail[:150]}")

    corrected = [r for r in results if r.entity_corrected]
    injected = [r for r in results if r.time_injected]
    if corrected or injected:
        print("\nNormalisation applied before compiling")
        if corrected:
            print(f"  {len(corrected):4d}  entity prefix rewritten as unreachable")
        if injected:
            print(f"  {len(injected):4d}  metric_time injected for an offset window")

    regressions: list[PairResult] = []
    # ── Attribution ─────────────────────────────────────────────────────────
    # Reported as information, not as a pass/fail tally. A high share is often
    # correct (see the note in the regression arm below), so the value of this
    # section is the LEVEL being visible and stable between runs, not a count of
    # things to fix.
    scored = [r for r in results if r.attribution in (_ATTR_OK, _ATTR_SKEWED)]
    if scored:
        skewed = sorted(
            (r for r in scored if r.attribution == _ATTR_SKEWED),
            key=lambda r: -(r.unattributed_pct or 0.0),
        )
        na = sum(1 for r in results if r.attribution == _ATTR_NA)
        print("\nAttribution (what share of the MEASURE lands in an unattributed bucket)")
        print(f"  {len(scored) - len(skewed):>4}  below {_ATTR_THRESHOLD_PCT:.0f}%")
        print(f"  {len(skewed):>4}  at or above {_ATTR_THRESHOLD_PCT:.0f}%")
        print(f"  {na:>4}  not measurable (ratio, average or count_distinct)")
        if skewed:
            print("  highest shares - check each is BY CONSTRUCTION, not a broken join:")
            for r in skewed[:10]:
                print(f"      {r.unattributed_pct:6.2f}%  {r.metric} x {r.bare}")
            if len(skewed) > 10:
                print(f"      ... and {len(skewed) - 10} more (see the CSV)")

    if previous:
        gained: list[PairResult] = []
        for r in results:
            was = previous.get((r.metric, r.bare))
            if was is None:
                continue
            was_ok, was_population, was_attribution = was
            if r.ok and not was_ok:
                gained.append(r)
            elif was_ok and not r.ok:
                regressions.append(r)
            # A pair that stops carrying DATA is a regression too, and a quieter
            # one: it still compiles, still returns a row, and the answer is just
            # empty. That is how the content_primary_genre problem reached a user.
            # Skipped when the baseline predates the column ("") — unknown is not
            # a change.
            elif (
                was_population
                and was_population not in _POP_FAILURES
                and r.population in _POP_FAILURES
            ):
                regressions.append(r)
            # And a pair whose measure DRAINS into the unattributed bucket. This is
            # the quietest of the three: it compiles, returns real buckets, and only
            # the volume behind them has gone missing. `total_revenue x country` sat
            # at 24.6% during the June-2026 orphan append while `population` scored
            # it `ok`.
            #
            # Keyed on the verdict CHANGING, not on the standing value, and that is
            # the whole design. Plenty of dimensions are legitimately sparse --
            # `mrr x churn_reason` is 80.7% unattributed because most MRR belongs to
            # active subscribers who have no churn reason -- so failing on the level
            # would report 19 permanent "problems" and be ignored within a week. A
            # by-construction null is STABLE; a broken join is a CHANGE.
            elif (
                was_attribution == _ATTR_OK
                and r.attribution == _ATTR_SKEWED
            ):
                regressions.append(r)
        print(f"\nAgainst the previous run: {len(gained)} newly passing, "
              f"{len(regressions)} regressed")
        for r in regressions:
            detail = r.reason or r.population
            if r.attribution == _ATTR_SKEWED and r.population not in _POP_FAILURES:
                detail = (f"{r.unattributed_pct:.1f}% of the measure is now "
                          f"unattributed")
            print(f"  [REGRESSED] {r.metric} x {r.bare} -> {detail}")

    return len(regressions)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verify every certified metric x dimension pair compiles."
    )
    ap.add_argument("--metric", action="append", default=None,
                    help="Limit to these metrics (repeatable).")
    ap.add_argument("--out", default=DEFAULT_OUT, help="CSV output path.")
    ap.add_argument("--include-internal", action="store_true",
                    help="Also probe ratio building blocks hidden from users.")
    ap.add_argument("--fail-on-new", action="store_true",
                    help="Exit 1 if a pair that used to compile, or used to carry "
                         "data, no longer does.")
    ap.add_argument("--no-execute", action="store_true",
                    help="Compile only; skip the population check. Use when the "
                         "warehouse file is unavailable (e.g. CI without the "
                         "release asset).")
    ap.add_argument("--verbose", action="store_true", help="Log every probe.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )

    print("Certified dimension coverage audit")
    print("  loading the live registry...")
    registry, settings = _load_registry()

    from core.sql_generator import build_dimension_prefix_map

    metrics = (registry.list_metrics() if args.include_internal
               else registry.list_user_facing_metrics())
    if args.metric:
        wanted = {m.lower() for m in args.metric}
        metrics = [m for m in metrics if m.name.lower() in wanted]
        missing = wanted - {m.name.lower() for m in metrics}
        if missing:
            sys.exit(f"Unknown metric(s): {sorted(missing)}")

    prefix_map = build_dimension_prefix_map()
    pairs = [(m.name, d, m.metric_type) for m in metrics for d in m.certified_dimensions]
    print(f"  {len(metrics)} metric(s), {len(pairs)} claimed pair(s)")
    print("  building the warm MetricFlow engine (one-off, 20-105s)...")

    engine = _build_engine(settings)
    previous = _read_previous(args.out)

    # Population check is on by default: a pair that compiles but returns an empty
    # dimension is indistinguishable from a working one in the compile matrix, and
    # that gap is what put a null genre breakdown in front of a user. It degrades to
    # compile-only rather than failing, because the compile pass needs only
    # semantic_manifest.json while this needs the (gitignored) warehouse file.
    con = None
    if args.no_execute:
        print("  population check SKIPPED (--no-execute)")
    else:
        con, why = _open_warehouse(settings)
        if con is None:
            print(f"  population check unavailable - {why}")
            print("  continuing with compile-only results")
        else:
            print("  population check enabled")

    # Resolved once from semantic_manifest.json — the same artifact the compile
    # pass already needs, so this adds no new dependency.
    # There is no settings field for the semantic manifest — MetricFlow is given
    # `dbt_project_dir` and finds target/ itself — so derive it the same way, rather
    # than adding a config field that would then have two sources of truth.
    additive = _additive_metrics(
        os.path.join(getattr(settings, "dbt_project_dir", ""),
                     "target", "semantic_manifest.json")
    ) if con else {}
    if con:
        print(f"  attribution check enabled for {len(additive)} additive metric(s)")

    summary = Summary()
    started = time.perf_counter()
    for i, (metric, bare, metric_type) in enumerate(pairs, 1):
        result = _probe(engine, metric, bare, prefix_map, metric_type,
                        con=con, additive=additive)
        summary.results.append(result)
        flagged = (not result.ok) or result.population in _POP_FAILURES
        if args.verbose or flagged:
            mark = "FAIL" if not result.ok else (
                "EMPTY" if result.population in _POP_FAILURES else "ok  ")
            note = result.reason or result.population
            print(f"  [{i:3d}/{len(pairs)}] {mark} {metric} x {bare}"
                  + (f"  ({note})" if note and note != _POP_OK else ""))
        elif i % 25 == 0:
            print(f"  [{i:3d}/{len(pairs)}] ...")

    elapsed = time.perf_counter() - started
    mean_ms = (sum(r.ms for r in summary.results) / len(summary.results)
               if summary.results else 0.0)
    print(f"\n  {len(pairs)} probes in {elapsed:.1f}s ({mean_ms:.0f} ms mean)")

    regressions = _report(summary, previous)
    _write_csv(args.out, summary.results)
    if con is not None:
        con.close()
    print(f"\nWrote {args.out}")
    print("Feed pairs that BOTH compile and are populated into "
          "diagnostics/driver_graph.yml, not the claimed list.")

    if args.fail_on_new and regressions:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
