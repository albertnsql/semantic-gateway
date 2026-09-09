"""
core/diagnostics/graph.py — the LangGraph wiring. The ONLY module that imports it.

Two consequences of that isolation, both deliberate:

* the engine below is testable with `langgraph` absent — every other module in this
  package has no idea it exists
* if a 200-line asyncio orchestrator later turns out to be enough, this is a one-file
  replacement rather than a rewrite

**Phase 2 is deterministic end to end: zero LLM calls.** That is a change from the
original design and it is the right one. Planning needs the metric (the existing
`IntentExtractor` already gives it), the dimensions (`driver_graph.yml` orders them)
and the windows (`windows.py` builds them) — none of which needs a model. Analysis is
arithmetic by construction. And `analysis.build_hypothesis()` already writes a plain
statement per hypothesis, so an answer can be assembled without generation.

Which matters because the binding constraint is Google's 15 requests/minute with no
working fallback: a diagnosis that costs 0 LLM calls does not compete with chat
traffic, and it is reproducible, which an LLM-planned one is not. Phase 3 adds an
optional prose pass on top — but the numbers and the strength claims will still
originate here, so the citation check stays meaningful rather than decorative.

`langgraph` costs 10.7 s of import and +68.6 MB RSS (measured), against ~251 MB
already used on a 512 MB instance. So `build_diagnostic_agent()` is called lazily and
memoised by the caller, never at import time.
"""

from __future__ import annotations

import logging
import operator
import threading
import time
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

if TYPE_CHECKING:  # pragma: no cover
    from langchain_core.runnables import RunnableConfig
else:
    # ── Node signatures must read exactly `config: RunnableConfig` ────────────
    # LangGraph decides whether to inject the config by matching the node's RAW
    # annotation string, and silently passes nothing when it does not match. The
    # node then receives None, every service lookup returns None, and it surfaces
    # as `'NoneType' object has no attribute 'validate'` from inside the graph —
    # which reads like a wiring bug rather than a typing one. Verified against
    # langgraph 1.2.11:
    #
    #     config: dict | None = None                -> NOT injected
    #     config: "RunnableConfig | None" = None     -> NOT injected
    #     config: RunnableConfig | None = None       -> NOT injected
    #     config: RunnableConfig = None              -> injected
    #
    # Note the third case: `typing.get_type_hints()` resolves it correctly and it
    # compares equal to `RunnableConfig | None`, so the annotation is not the
    # problem — the string match is. LangGraph's own warning names
    # 'RunnableConfig | None' as acceptable and then rejects it, so do not trust
    # the message; keep the bare form. `= None` stays so the nodes can be called
    # directly in tests.
    #
    # Aliasing to dict when langchain_core is absent keeps this module importable
    # (and its nodes unit-testable) without langgraph installed, which is the whole
    # reason the langgraph import is confined to build_diagnostic_agent().
    try:
        from langchain_core.runnables import RunnableConfig
    except ImportError:  # pragma: no cover
        RunnableConfig = dict

from core.diagnostics.analysis import (
    DEFAULT_MIN_ABSOLUTE,
    baseline_representativeness,
    leading_driver,
    build_hypothesis,
    decompose,
    is_broad_based,
    rank_hypotheses,
    reconciles_with_baseline,
    rows_to_buckets,
)
from core.diagnostics.playbooks import (
    DriverGraph,
    ProbePlan,
    pair_findings,
    plan_time_comparison,
    usable_pairs,
)
from core.diagnostics.state import Budget, Finding, Hypothesis
from core.diagnostics.tools import ProbeRejected, build_probe_intent, run_governed_query
from core.diagnostics.artifacts import ArtifactRegistry
from core.diagnostics.windows import Window, describe_comparison

logger = logging.getLogger(__name__)


class GraphState(TypedDict, total=False):
    """
    State threaded through the graph.

    `findings` carries an additive reducer because `execute_probes` appends from a
    loop that Phase 3 will fan out concurrently. Without it LangGraph rejects
    concurrent writes to one key with InvalidUpdateError — and adding the reducer
    later, once the fan-out exists, means debugging it under concurrency instead of
    now.
    """

    question: str
    request_id: str
    metric: str
    target: Window
    comparison: Window | None
    filters: list[Any]
    max_dimensions: int
    plan: ProbePlan | None
    findings: Annotated[list[Finding], operator.add]
    hypotheses: list[Hypothesis]
    budget: Budget
    answer: str
    # The bottom line, kept separate from `answer` so the UI can lead with a
    # conclusion rather than the full linear prose. See synthesize_node.
    summary: str
    stopped_because: str
    # ── reflect-loop bookkeeping ────────────────────────────────────────────
    # Dimensions already asked about, so a second round asks something NEW rather
    # than re-selecting the same top-N and burning the round cap on an identical
    # plan. NOT an additive reducer: plan_node rewrites it wholesale, and an
    # additive one would accumulate duplicates across rounds.
    probed_dimensions: list[str]
    # How many axes the driver graph still has left. Computed in plan_node so the
    # conditional edge can decide without needing the DriverGraph — a plain
    # function on the edge does not get services injected the way a node does.
    dimensions_remaining: int


def _services(config: RunnableConfig) -> dict:
    """
    Pull the gateway services out of the LangGraph config.

    Passed per-invocation rather than captured at build time so one compiled graph
    serves every request, matching how `app.state` services are shared.
    """
    return (config or {}).get("configurable", {})


def _weight_availability(services: dict):
    """
    Callable telling the planner whether a weight metric certifies a dimension.

    Returns None when no registry was supplied, which makes the planner keep its
    previous behaviour (plan the weight probe and let the validator reject it) rather
    than silently dropping every weight.
    """
    registry = services.get("registry")
    if registry is None:
        return None

    def available(weight_metric: str, dimension: str) -> bool:
        try:
            return bool(registry.is_certified_dimension(weight_metric, dimension))
        except Exception:
            return False

    return available


# ─────────────────────────────────────────────────────────── nodes

def plan_node(state: GraphState, config: RunnableConfig = None) -> dict:
    """
    Build the probe plan. Deterministic — same question, same probes.

    Raises nothing on an unknown metric: it records why it stopped, because a
    diagnosis that cannot be planned should answer "I have no playbook for that"
    rather than surface a KeyError as a 500.
    """
    graph: DriverGraph = _services(config).get("driver_graph") or DriverGraph.load()
    metric = state["metric"]
    already = list(state.get("probed_dimensions") or [])
    previous: ProbePlan | None = state.get("plan")
    try:
        plan = plan_time_comparison(
            metric,
            state["target"],
            graph=graph,
            comparison=state.get("comparison"),
            max_dimensions=state.get("max_dimensions", 3),
            filters=state.get("filters") or [],
            weight_available=_weight_availability(_services(config)),
            exclude_dimensions=already,
        )
    except KeyError as exc:
        logger.info("no playbook for %s: %s", metric, exc)
        return {
            "plan": None,
            "stopped_because": f"no diagnostic playbook exists for '{metric}'",
        }

    budget = state.get("budget") or Budget()

    # ── merge BEFORE trimming ───────────────────────────────────────────────
    # `pair_findings()` iterates plan.probes, so a plan holding only the NEW
    # dimensions would drop round one's pairs and lose its hypotheses. The plan
    # therefore stays CUMULATIVE, and `execute_probes_node` skips whatever already
    # has a finding — which also makes re-entering this node idempotent.
    #
    # Only probes carrying a `dimension` are carried over. The baseline/comparison/
    # trend trio has none and is already answered; re-adding it would re-derive the
    # same totals.
    if previous is not None and already:
        fresh = [pr for pr in plan.probes if pr.dimension]
        plan = ProbePlan(
            metric=previous.metric,
            target=previous.target,
            comparison=previous.comparison,
            probes=previous.probes + fresh,
            dimensions=previous.dimensions + plan.dimensions,
            cautions=previous.cautions,
            weight_metric=previous.weight_metric,
            trend=previous.trend,
            # Deduplicated, order preserved. Most notes are properties of the METRIC
            # rather than of the round -- "net_mrr_growth has no weight_metric" is
            # re-derived identically every time plan_probes() runs -- so a plain
            # concatenation printed it once per reflect round. A live two-round
            # answer carried the same sentence twice, which reads as two separate
            # problems.
            notes=_dedupe(previous.notes + plan.notes),
        )

    # ── trim only what has NOT been run ─────────────────────────────────────
    # The budget bounds remaining WORK, and on a reflect round most of the plan is
    # already executed. Comparing the cumulative length against `probes_remaining`
    # would trim round one's probes away: churn_rate plans 15 and the default cap is
    # 16, so round two saw `probes_remaining == 1` and would have cut a 27-probe
    # cumulative plan down to a single baseline probe — discarding every finding's
    # pairing and, because that survivor carries no dimension, adding no new axis
    # either. Silent, and it would have looked like the loop simply found nothing.
    answered = {f.label for f in (state.get("findings") or []) if f.ok}
    done = [pr for pr in plan.probes if pr.label in answered]
    pending = [pr for pr in plan.probes if pr.label not in answered]
    if len(pending) > budget.probes_remaining:
        # Trim rather than refuse: a two-dimension diagnosis beats none.
        keep = pending[: budget.probes_remaining]
        logger.info(
            "probe budget trims %d pending probe(s) to %d", len(pending), len(keep)
        )
        plan = ProbePlan(
            metric=plan.metric, target=plan.target, comparison=plan.comparison,
            probes=done + keep, dimensions=plan.dimensions, cautions=plan.cautions,
            weight_metric=plan.weight_metric,
            # `trend` must be carried, or a trimmed plan silently loses the baseline
            # representativeness check and the answer stops warning about an
            # unrepresentative comparison without saying why.
            trend=plan.trend,
            notes=_dedupe(plan.notes + ["the probe budget trimmed this plan"]),
        )

    budget.rounds_used += 1
    # Axes the driver graph still holds. Compared on the plan's own dimension
    # strings, which is what `exclude_dimensions` will be given next round.
    remaining = [
        d for d in graph.ordered_dimensions(metric)
        if d not in plan.dimensions
    ]

    logger.info(
        "round %d: %d probe(s) planned for %s over %s vs %s (%d axis/axes left)",
        budget.rounds_used, len(plan.probes), metric, plan.target, plan.comparison,
        len(remaining),
    )
    return {
        "plan": plan,
        "budget": budget,
        "probed_dimensions": list(plan.dimensions),
        "dimensions_remaining": len(remaining),
    }


def execute_probes_node(state: GraphState, config: RunnableConfig = None) -> dict:
    """
    Run every planned probe through the governed path.

    Serial on purpose. Eight probes measured 421 ms warm, so concurrency buys
    almost nothing here and costs the reducer-and-ordering complexity that Phase 3
    can take on if per-probe progress streaming turns out to matter.
    """
    plan: ProbePlan | None = state.get("plan")
    if plan is None:
        return {}

    services = _services(config)
    validator = services.get("validator")
    sql_generator = services.get("sql_generator")
    query_cache = services.get("query_cache")
    budget = state.get("budget") or Budget()

    # Findings already gathered by an earlier round. Two reasons this matters:
    #   * a probe whose label is already answered is SKIPPED, so a reflect round
    #     costs only its new axes rather than re-running the whole cumulative plan;
    #   * `run_governed_query` derives finding ids from `existing`, so seeding it
    #     keeps them unique. Starting from an empty list would mint a second F1 and
    #     the answer's [F1] citation would point at two different probes.
    prior: list[Finding] = list(state.get("findings") or [])
    answered = {f.label for f in prior if f.ok}

    findings: list[Finding] = []
    # The clock starts AFTER the first probe. The first one can pay the warm
    # MetricFlow engine build — measured at 43 s cold — which is one-off
    # infrastructure warm-up, not diagnosis work. Timing it defeated the deadline
    # immediately: a live `total_revenue` run completed 1 of 14 probes and reported
    # "Stopped early: reached the 25s deadline", while the very next diagnosis in
    # the same process ran all 14 in under a second. A probe already in flight
    # cannot be cancelled anyway, so counting it only mislabels the cause.
    started: float | None = None
    for probe in plan.probes:
        if probe.label in answered:
            continue
        if started is not None and budget.spent:
            logger.info("budget spent mid-plan: %s", budget.why_spent())
            break
        intent = build_probe_intent(
            probe.metric, probe.dimensions,
            time_range=probe.window.as_time_range(), filters=probe.filters,
            original_query=state.get("question", ""),
        )
        try:
            finding = run_governed_query(
                intent, validator=validator, sql_generator=sql_generator,
                existing=prior + findings, label=probe.label,
                query_cache=query_cache,
            )
        except ProbeRejected as exc:
            # Governance said no. That is a driver-graph bug, not a data problem, so
            # record it as a failed finding and keep going rather than aborting.
            logger.warning("probe rejected: %s", exc)
            finding = Finding(
                id=f"F{len(prior) + len(findings) + 1}", label=probe.label,
                metric=probe.metric, dimensions=probe.dimensions, rows=[],
                error=str(exc),
            )
        findings.append(finding)
        budget.probes_used += 1
        if started is None:
            started = time.perf_counter()   # the engine is warm from here on
        else:
            budget.seconds_used = time.perf_counter() - started

    ok = sum(1 for f in findings if f.ok)
    logger.info(
        "executed %d new probe(s), %d ok, %d already answered, in %.0f ms "
        "after warm-up",
        len(findings), ok, len(answered), budget.seconds_used * 1000,
    )
    return {"findings": findings, "budget": budget}


def analyze_node(state: GraphState, config: RunnableConfig = None) -> dict:
    """
    Decompose every usable dimension pair. Pure arithmetic — no model involved.

    This is the node that makes a causal claim defensible, so it does the least
    interesting-looking work in the graph and carries the most weight.
    """
    plan: ProbePlan | None = state.get("plan")
    findings = state.get("findings") or []
    if plan is None or not findings:
        return {"hypotheses": []}

    grouped = pair_findings(plan, findings)
    hypotheses: list[Hypothesis] = []

    # The movement every hypothesis is supposed to be explaining. Passed into
    # build_hypothesis so a decomposition whose buckets do not reproduce this gap is
    # reported as inconclusive rather than as a cause -- the net_mrr_growth case,
    # where summing a growth RATE over (month x bucket) gave +26.21 against a real
    # movement of -1.36 and was badged "explains" with the opposite sign.
    baseline = _baseline_gap(plan, findings)
    baseline_gap = baseline[2] if baseline is not None else None

    for dim in usable_pairs(grouped):
        roles = grouped[dim]
        qualified = next(
            (d for d in roles["target"].dimensions), dim
        )
        value_column = plan.metric
        try:
            target_buckets = rows_to_buckets(
                roles["target"].rows, qualified, value_column
            )
            comparison_buckets = rows_to_buckets(
                roles["comparison"].rows, qualified, value_column
            )
        except KeyError as exc:
            logger.warning("cannot read %s from the probe result: %s", dim, exc)
            continue

        # Weights turn an additive decomposition into a mix-vs-rate one. Both sides
        # must have them or `decompose()` correctly falls back and says so.
        if "weight_target" in roles and "weight_comparison" in roles:
            target_buckets = _attach_weights(
                target_buckets, roles["weight_target"], qualified, plan.weight_metric
            )
            comparison_buckets = _attach_weights(
                comparison_buckets, roles["weight_comparison"], qualified,
                plan.weight_metric,
            )

        result = decompose(dim, target_buckets, comparison_buckets)
        evidence = [roles["target"].id, roles["comparison"].id]
        hypotheses.append(
            build_hypothesis(result, evidence=evidence, baseline_gap=baseline_gap)
        )

    ranked = rank_hypotheses(hypotheses)
    logger.info(
        "analysed %d dimension(s): %s",
        len(ranked), ", ".join(f"{h.dimension}={h.verdict}" for h in ranked) or "none",
    )
    return {"hypotheses": ranked}


def _attach_weights(buckets, weight_finding, dimension_column, weight_metric):
    """
    Join weights onto value buckets by label.

    A bucket with no matching weight keeps `weight=None`, which makes
    `decompose()` fall back to additive for the whole dimension rather than
    silently weighting some buckets and not others.
    """
    from core.diagnostics.state import Bucket

    try:
        weight_buckets = rows_to_buckets(
            weight_finding.rows, dimension_column, weight_metric
        )
    except KeyError:
        return buckets
    by_label = {w.label: w.value for w in weight_buckets}
    return [Bucket(b.label, b.value, by_label.get(b.label)) for b in buckets]


# How many hypotheses the answer enumerates before summarising the rest. Three
# keeps a reflect-loop answer the same length as a single-round one, so extra
# rounds buy accuracy rather than verbosity.
_MAX_REPORTED_HYPOTHESES = 3


def describe_movement(
    metric: str,
    target_total: float,
    comparison_total: float,
    gap: float,
    comparison_phrase: str,
) -> str:
    """
    The headline sentence: what moved, by how much, against what.

    A percent change is only interpretable when the baseline is non-zero AND the
    two sides share a sign. `net_mrr_growth` for 2026 vs 2025 went from +0.58 to
    -0.78 -- a swing from growth to decline -- and dividing by the baseline
    reported "down 234.6%", which reads as a catastrophe of a magnitude the numbers
    do not contain. Worse, it hides the only thing that actually happened: the sign
    flipped.

    So a sign change is described as a sign change, a zero baseline gets the
    absolute move, and an ordinary same-sign change keeps the percentage it has
    always had.
    """
    direction = "down" if gap < 0 else "up"
    # Zero is tested BEFORE the signs: `0.0 > 0` is False, so a 0 -> 50 move read as
    # a sign flip and was described as "turned from decline to growth" off a
    # baseline that was never negative.
    if not comparison_total:
        return (
            f"{metric} is {direction} by {abs(gap):,.2f} against "
            f"{comparison_phrase} ({target_total:,.2f} vs "
            f"{comparison_total:,.2f}). The baseline is zero, so there is no "
            f"percentage to quote."
        )

    sides_differ = (target_total > 0) != (comparison_total > 0)

    if not sides_differ:
        pct = gap / comparison_total * 100
        return (
            f"{metric} is {direction} {abs(pct):.1f}% against {comparison_phrase} "
            f"({target_total:,.2f} vs {comparison_total:,.2f})."
        )

    # A sign flip. The percentage is unusable and the sign change IS the finding.
    moved = "from growth to decline" if comparison_total > 0 else "from decline to growth"
    return (
        f"{metric} turned {moved} against {comparison_phrase}: "
        f"{comparison_total:,.2f} to {target_total:,.2f}, a move of {gap:+,.2f}. "
        f"The two periods have opposite signs, so a percentage change is not "
        f"meaningful here."
    )


def _unreconciled(
    hypotheses: list[Hypothesis], baseline_gap: float | None
) -> list[Hypothesis]:
    """
    The axes refused because the metric does not add up across segments.

    `inconclusive` covers three unrelated situations -- a residual too large to
    trust, a gap of ~0, and a decomposition that does not reproduce the baseline
    movement -- and only the third means "this metric cannot be broken down". They
    are told apart by the decomposition rather than by the verdict, the same
    distinction the reflect loop makes for the same reason: a zero-gap axis
    described as "does not add up across segments" is simply false, and a live test
    fixture with a flat 100-vs-100 comparison caught exactly that.
    """
    if baseline_gap is None:
        return []
    return [
        h for h in hypotheses
        if h.verdict == "inconclusive"
        and h.decomposition is not None
        and not reconciles_with_baseline(h.decomposition, baseline_gap)
    ]


def _bottom_line(
    plan: ProbePlan, hypotheses: list[Hypothesis], baseline_gap: float | None = None
) -> str:
    """
    The verdict in one plain sentence, with no citations and no jargon.

    This is the line that answers "so what was the issue", and it exists because
    the previous answer never stated one. A live diagnosis opened with a signed
    percentage, then a hypothesis sentence carrying four clauses and a lift ratio,
    then five ruled-out axes, two duplicated notes and a caveat -- every fact a
    reader needed was present and none of them was the conclusion.

    Says "I could not attribute this" whenever that is the truth. An unattributable
    movement stated plainly is more useful than the largest bucket dressed up as a
    cause, which is the failure mode `leading_driver()` and the reconciliation check
    both exist to prevent.
    """
    explaining = [h for h in hypotheses if h.verdict in ("explains", "partial")]
    ruled_out = [h for h in hypotheses if h.verdict == "not_it"]
    unusable = _unreconciled(hypotheses, baseline_gap)

    if not hypotheses:
        return (
            "No breakdown was available, so this movement could not be attributed "
            "to any segment."
        )

    # Every axis unusable. Almost always one cause, and it is worth naming: the
    # metric cannot be summed across segments, so no decomposition of it is valid.
    # This is `net_mrr_growth`, where the buckets summed to +26.21 against a real
    # move of -1.36 -- and the old code reported that as an explanation.
    if unusable and not explaining and not ruled_out:
        return (
            f"I could not attribute this. {plan.metric} does not add up across "
            f"segments, so breaking it down by "
            f"{_and_list([h.dimension for h in unusable])} does not reproduce the "
            f"movement and no share of it can be trusted. The totals above are "
            f"correct; only the attribution is unavailable."
        )

    if is_broad_based(hypotheses):
        return (
            f"No single segment is behind this. The movement is spread across "
            f"every axis I checked "
            f"({_and_list([h.dimension for h in hypotheses])}) -- on each one, no "
            f"value carries meaningfully more of it than its own size implies."
        )

    if explaining:
        lead = max(explaining, key=_top_share)
        top = lead.decomposition.top if lead.decomposition else None
        label = "(not set)" if top is None or top.label is None else top.label
        share = _top_share(lead)
        checked = ""
        if ruled_out:
            checked = (
                f" {_and_list([h.dimension for h in ruled_out])} "
                f"{'were' if len(ruled_out) > 1 else 'was'} checked and ruled out."
            )
        strength = "explains most of" if lead.verdict == "explains" else "is the largest part of"
        return (
            f"The clearest driver is {lead.dimension} = {label}, which "
            f"{strength} the movement at {share:.0%} of it.{checked}"
        )

    return (
        f"Nothing I checked explains this. On "
        f"{_and_list([h.dimension for h in hypotheses])}, no value carries "
        f"meaningfully more of the movement than its own size implies."
    )


def _and_list(items: list[str]) -> str:
    """`a`, `a and b`, `a, b and c` -- prose, not a comma-joined dump."""
    items = [i for i in items if i]
    if not items:
        return "no dimension"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _dedupe(items: list[str]) -> list[str]:
    """Drop repeats, keep first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _top_share(hypothesis: Hypothesis) -> float:
    """Share of the gap held by this axis's largest single mover.

    The quantity the statements quote, and therefore the one the reader compares.
    Distinct from `explained_share`, which is the decomposition's total coverage.
    """
    decomposition = hypothesis.decomposition
    if decomposition is None or not decomposition.gap:
        return 0.0
    top = decomposition.top
    if top is None:
        return 0.0
    return abs(top.delta / decomposition.gap)


def should_reflect(state: GraphState) -> str:
    """
    After analysis: try another set of axes, or write the answer.

    Returns the next node name. Four gates, and every one of them is a way the
    loop could otherwise do harm rather than good:

    1. **Something already explains it.** Judged by `is_broad_based()`, NOT by the
       verdict. That distinction is the whole reason the loop is useful: `partial`
       is not an established cause, it is "this axis leads a bit", and every one of
       the nine snapshot scenarios returns `partial` on every axis. Gating on
       verdict in (`explains`, `partial`) — which is what I wrote first — meant the
       loop could never fire on any real diagnosis.

       `is_broad_based()` already encodes the right question, and synthesize
       already uses it to say "no single value accounts for as much as half of it".
       When that is true nothing has been isolated, so another set of axes is worth
       asking for. When it is false something concentrated was found, and more
       probing only lengthens an answer that already has its cause.

    2. **There is nothing to explain.** This is the gate that is easy to miss.
       `build_hypothesis()` returns `inconclusive` for TWO different situations —
       a residual too large to trust, and `abs(gap) < DEFAULT_MIN_ABSOLUTE`, i.e.
       the metric barely moved. Looping on the second is pure waste: no axis can
       decompose a gap that is not there, so it would spend the full round and
       probe budget to conclude the same "no material gap" three times. Told apart
       by the decomposition's own gap, not by the verdict.

    3. **Budget.** `spent` covers rounds, probes and the deadline together, so the
       loop cannot outrun any of the three.

    4. **Axes left.** With no unprobed dimension remaining, a further round would
       plan zero new probes and re-analyse identical findings forever. This is the
       termination guarantee, and it does not depend on the others holding.

    A plain function, not a node: it must not write state, and keeping it pure
    means the decision is testable without a graph or a warehouse.
    """
    if state.get("plan") is None:
        return "synthesize"

    hypotheses = state.get("hypotheses") or []
    if not hypotheses:
        return "synthesize"

    # (1) the metric did not materially move
    material = any(
        h.decomposition is not None
        and abs(h.decomposition.gap) >= DEFAULT_MIN_ABSOLUTE
        for h in hypotheses
    )
    if not material:
        logger.info("no material gap to explain - not reflecting")
        return "synthesize"

    # (2) something concentrated was found — a cause is established
    #
    # Tested on `concentration()` against the same DEFAULT_DOMINANT_SHARE that
    # `is_broad_based()` uses, so the loop and the answer agree on what "leads the
    # movement" means. Deliberately NOT `is_broad_based()` itself: that requires at
    # least two axes ("one slice says nothing about breadth"), which is right for
    # deciding what to REPORT and wrong here — a diagnosis that examined one axis
    # and found nothing is precisely the one that should try another, and
    # `is_broad_based()` returns False for it, which would stop the loop.
    graded = [
        h for h in hypotheses
        if h.decomposition is not None and h.verdict != "inconclusive"
    ]
    # An over-contributing driver, not merely a large share. Gating on share alone
    # made the loop keep probing until it reached a two-bucket axis and then declare
    # it the cause: `billing_cycle = monthly` at 76.1% of the gap is 72.2% of the
    # base, a lift of 1.05, i.e. proportional. Four of nine snapshot scenarios
    # flipped to "found the cause" on exactly that artifact.
    if any(leading_driver(h.decomposition) is not None for h in graded):
        return "synthesize"

    budget = state.get("budget") or Budget()
    # (3) rounds / probes / deadline
    if budget.spent:
        logger.info("not reflecting: %s", budget.why_spent())
        return "synthesize"

    # (4) nothing new left to ask
    if not state.get("dimensions_remaining"):
        logger.info("not reflecting: no unprobed dimension remains")
        return "synthesize"

    logger.info(
        "reflecting: %d axis/axes examined, movement still broad-based, "
        "%d axis/axes left to try",
        len(hypotheses), state.get("dimensions_remaining", 0),
    )
    return "plan"


def synthesize_node(state: GraphState, config: RunnableConfig = None) -> dict:
    """
    Assemble the answer from what the arithmetic produced.

    Deterministic in Phase 2. Every sentence traces to a `Hypothesis.statement`
    generated by `analysis.build_hypothesis()`, so nothing here can assert a cause
    the numbers do not support — which is the whole reason the diagnostic path is
    allowed to say "because" when the narrative path is not.
    """
    plan: ProbePlan | None = state.get("plan")
    hypotheses = state.get("hypotheses") or []
    stopped = state.get("stopped_because", "")

    if plan is None:
        text = stopped or "This question could not be diagnosed."
        return {"answer": text, "summary": text}

    lines: list[str] = []
    # The summary shares its sentences with the full prose rather than rewording
    # them, so the two can never disagree about what the numbers were.
    summary_lines: list[str] = []
    baseline = _baseline_gap(plan, state.get("findings") or [])
    comparison_phrase = describe_comparison(plan.target, plan.comparison)
    if baseline is not None:
        target_total, comparison_total, gap = baseline
        headline = describe_movement(
            plan.metric, target_total, comparison_total, gap, comparison_phrase
        )
    else:
        headline = (
            f"Comparing {plan.metric} against {comparison_phrase}; the baseline "
            "totals could not be established."
        )
    lines.append(headline)
    summary_lines.append(headline)

    # A gap is only as meaningful as what it is measured against. A live June churn
    # diagnosis reported +163.7% against a May that was itself 41% below the trailing
    # 12-month average; against trend the figure is +55%. Both correct, one useful.
    # This says so rather than swapping the baseline for another guessable one.
    baseline_gap = baseline[2] if baseline is not None else None
    check = _baseline_check(plan, state.get("findings") or [], baseline)
    if check is not None and not check.representative:
        message = (
            f"Read that against trend: the comparison period was itself "
            f"{abs(check.deviation):.0%} {check.direction} the trailing "
            f"{check.trend_months}-month average, so it is not a typical baseline"
        )
        if check.trend_relative_gap is not None:
            message += (
                f". Against that average the target is "
                f"{check.trend_relative_gap:+.0%}"
            )
        lines.append(message + ".")
        summary_lines.append(message + ".")

    explaining = [h for h in hypotheses if h.verdict in ("explains", "partial")]
    ruled_out = [h for h in hypotheses if h.verdict == "not_it"]
    unusable = _unreconciled(hypotheses, baseline_gap)

    if is_broad_based(hypotheses):
        # Three weak partials is honest and useless. "Spread across every axis" IS
        # the finding, and saying it is more useful than listing the largest bucket
        # on each axis as though any of them were the cause.
        axes = ", ".join(h.dimension for h in hypotheses)
        citations = sorted({e for h in hypotheses for e in h.evidence})
        # Says only what is_broad_based() actually checked. An earlier draft added
        # "and each axis needs several values to cover 80% of the gap", which was
        # true of the buckets-to-80% gate that got removed as cardinality-dependent
        # -- leaving the sentence asserting something no longer tested.
        lines.append(
            f"The movement is broad-based rather than concentrated: no single value "
            f"of {axes} accounts for as much as half of it. [{', '.join(citations)}]"
        )
        # The largest mover on each axis is still worth naming as context, clearly
        # subordinate to the verdict above rather than presented as a cause.
        for h in hypotheses:
            top = h.decomposition.top if h.decomposition else None
            if top is None or not h.decomposition.gap:
                continue
            share = abs(top.delta / h.decomposition.gap)
            label = "(not set)" if top.label is None else top.label
            lines.append(
                f"  - largest single mover by {h.dimension}: {label} at "
                f"{share:.1%} of the gap"
            )
    elif explaining:
        # Ordered by TOP SHARE, and capped.
        #
        # `rank_hypotheses()` orders by `explained_share` — how much of the gap the
        # decomposition accounts for in total — which is ~1.0 for almost every
        # additive decomposition, so among partials the sort collapses onto its
        # tie-break and the displayed order carries no strength information. That
        # was survivable at three axes and is not at nine: a churn diagnosis listed
        # `country = AU at 10.1%` two lines above `plan_type = standard at 47.8%`,
        # while every statement quotes its own top share, so the reader is invited
        # to compare numbers the ordering contradicts.
        #
        # Sorted here rather than in `rank_hypotheses()` on purpose: that ordering
        # feeds the API payload and the frontend panel, and changing it is a
        # separate decision from how this sentence list reads.
        ordered = sorted(explaining, key=lambda h: -_top_share(h))
        for h in ordered[:_MAX_REPORTED_HYPOTHESES]:
            lines.append(f"{h.statement} [{', '.join(h.evidence)}]")
        rest = ordered[_MAX_REPORTED_HYPOTHESES:]
        if rest:
            # Named, not hidden. The reflect loop can examine nine axes, and
            # enumerating all of them buries the finding it exists to surface —
            # but silently dropping evidence is worse, so the weaker axes are
            # summarised with the largest share among them.
            biggest = _top_share(rest[0])
            lines.append(
                f"  - also examined {', '.join(h.dimension for h in rest)}: "
                f"none accounts for more than {biggest:.1%} of the gap"
            )
    elif unusable and not ruled_out:
        # Every axis refused, so none of them was CHECKED in the sense the branch
        # below claims. Saying "no value carries meaningfully more of the movement
        # than its own size implies" would assert a result the arithmetic never
        # produced -- the decomposition was rejected before any share was computed.
        lines.append(
            f"No attribution is available. {plan.metric} does not add up across "
            f"segments, so decomposing it by "
            f"{_and_list([h.dimension for h in unusable])} does not reproduce the "
            f"movement above and no share of it would be trustworthy."
        )
        for h in unusable:
            lines.append(f"  - {h.statement} [{', '.join(h.evidence)}]")
    else:
        checked = ", ".join(h.dimension for h in hypotheses) or "no dimension"
        # Wording matches what is actually tested. It used to say "none accounts for
        # a material share of the gap", which described the old share-only rule; the
        # test is now share AND lift, so a large-but-proportional bucket lands here
        # and "no material share" would be false of it.
        lines.append(
            f"No single factor explains it. I checked {checked}, and no value on "
            "any of them carries meaningfully more of the movement than its own "
            "size implies."
        )

    # Stating what was ruled out is often the most useful line in the answer, and
    # it costs nothing extra to produce.
    if ruled_out and explaining:
        lines.append(
            "Ruled out: " + "; ".join(f"{h.dimension} [{', '.join(h.evidence)}]"
                                      for h in ruled_out) + "."
        )

    for note in plan.notes:
        lines.append(f"Note: {note}.")
    for caution in plan.cautions:
        lines.append(f"Caveat: {caution}")

    # Window-scoped warnings about the DATA, as opposed to the per-metric cautions
    # above which are about the metric. Only those whose window actually intersects
    # this diagnosis are emitted -- a live answer once warned about the trailing
    # churn-only month while diagnosing June, which was the most alarming line in it
    # and did not apply.
    registry = _services(config).get("artifact_registry") or ArtifactRegistry.load()
    for artifact in registry.applicable(plan.metric, plan.target, plan.comparison):
        label = "Data warning" if artifact.severity == "high" else "Data note"
        text = artifact.summary
        if artifact.guidance:
            text = f"{text} {artifact.guidance}"
        lines.append(f"{label}: {text}")

    budget = state.get("budget")
    if budget is not None and budget.spent:
        lines.append(f"Stopped early: {budget.why_spent()}.")

    # One sentence saying what the reader came for: did we find a cause or not.
    # Deliberately the LAST thing built and the SECOND thing shown, because it has
    # to be phrased against the same hypothesis set the sections below it render.
    summary_lines.append(_bottom_line(plan, hypotheses, baseline_gap))

    # `answer` stays the complete linear text, because a text-only consumer
    # (the API, a log, a client without the panel) must still get everything.
    #
    # `summary` is the BOTTOM LINE, and it exists because the panel renders the
    # hypotheses, the ruled-out list, the notes, the cautions and the data
    # warnings as their own sections -- so putting the full prose above them
    # showed the reader every one of those things TWICE. A live answer repeated
    # its data warning verbatim in a red box and again six lines down, restated
    # its one finding under an EXPLAINS badge that already carried it, and
    # listed five ruled-out axes immediately above a section headed "checked
    # and ruled out". The panel renders this instead and keeps `answer` behind
    # a disclosure.
    return {
        "answer": "\n".join(lines),
        "summary": "\n".join(summary_lines),
        "stopped_because": stopped,
    }


def _baseline_check(plan: ProbePlan, findings: list[Finding], baseline):
    """
    Judge whether the comparison window was typical of recent history.

    Only for a single-month comparison: that is where an unrepresentative baseline
    distorts the headline, and it keeps the arithmetic exact. Returns None whenever a
    verdict cannot be supported — no trend rows, a multi-month comparison, unknown
    totals — because "could not tell" and "was typical" read identically in an answer
    and only one of them is honest.
    """
    if plan.trend is None or baseline is None:
        return None
    if plan.comparison.months_spanned != 1:
        return None

    by_label = {f.label: f for f in findings if f.ok}
    trend_finding = by_label.get(f"{plan.metric}, trend window")
    if trend_finding is None or not trend_finding.rows:
        return None

    key = next(
        (k for k in trend_finding.rows[0] if k.lower() == plan.metric.lower()), None
    )
    if key is None:
        return None

    # One row per month, because the trend probe groups by metric_time__month.
    monthly = [
        float(r[key]) for r in trend_finding.rows if r.get(key) is not None
    ]
    target_total, comparison_total, _ = baseline
    return baseline_representativeness(
        comparison_value=comparison_total,
        trend_monthly=monthly,
        target_value=target_total if plan.target.months_spanned == 1 else None,
    )


def _baseline_gap(plan: ProbePlan, findings: list[Finding]):
    """Total for each side, read from the no-breakdown baseline pair."""
    by_label = {f.label: f for f in findings if f.ok}
    target = by_label.get(f"{plan.metric}, target window")
    comparison = by_label.get(f"{plan.metric}, comparison window")
    if target is None or comparison is None:
        return None

    def total(finding: Finding) -> float | None:
        if not finding.rows:
            return None
        key = next(
            (k for k in finding.rows[0] if k.lower() == plan.metric.lower()), None
        )
        if key is None:
            return None
        return sum(float(r[key]) for r in finding.rows if r.get(key) is not None)

    t, c = total(target), total(comparison)
    if t is None or c is None:
        return None
    return t, c, t - c


# ─────────────────────────────────────────────────────────── assembly

def build_diagnostic_agent(checkpointer: Any | None = None) -> Any:
    """
    Compile the graph. Call lazily and memoise — the import alone is 10.7 s and
    +68.6 MB.

    plan -> execute -> analyze, then analyze either loops back to plan or falls
    through to synthesize. The loop is what lets an inconclusive first pass try the
    NEXT axes rather than answering "I cannot tell" while the driver graph still
    held dimensions it never asked about.

    Termination does not rest on the budget alone. `should_reflect()` also stops
    when no unprobed dimension remains, so even a misconfigured `max_rounds` cannot
    spin: each round consumes axes from a finite list and the planner excludes what
    has already been asked.
    """
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(GraphState)
    builder.add_node("plan", plan_node)
    builder.add_node("execute_probes", execute_probes_node)
    builder.add_node("analyze", analyze_node)
    builder.add_node("synthesize", synthesize_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute_probes")
    builder.add_edge("execute_probes", "analyze")
    # The reflect loop. `analyze` routes back to `plan` when a real gap exists and
    # nothing found so far clears the effect-size floor — so an inconclusive first
    # pass tries the NEXT axes instead of answering "I cannot tell" while the driver
    # graph still had dimensions it never asked about. See should_reflect() for the
    # four gates, one of which (no unprobed dimension left) is the termination
    # guarantee and does not depend on the budget.
    builder.add_conditional_edges(
        "analyze",
        should_reflect,
        {"plan": "plan", "synthesize": "synthesize"},
    )
    builder.add_edge("synthesize", END)

    return builder.compile(checkpointer=checkpointer)


_AGENT: Any = None
_AGENT_ATTEMPTED = False
_AGENT_LOCK = threading.Lock()


def get_diagnostic_agent() -> Any:
    """
    Return the compiled graph, building it on first need. Memoised.

    Both success AND failure are cached, matching `SQLGenerator._get_warm_engine()`:
    a deployment where langgraph cannot import must not re-pay the 10.7s attempt on
    every diagnostic question. The lock makes the build happen once even when several
    `anyio` worker threads arrive together.

    Returns None if langgraph is unavailable, so the caller degrades to the
    out-of-scope reply rather than 500-ing.
    """
    global _AGENT, _AGENT_ATTEMPTED

    if _AGENT_ATTEMPTED:
        return _AGENT

    with _AGENT_LOCK:
        if _AGENT_ATTEMPTED:  # another thread built it while we waited
            return _AGENT
        _AGENT_ATTEMPTED = True

        from core import memory

        rss_before = memory.rss_mb()
        started = time.perf_counter()
        try:
            _AGENT = build_diagnostic_agent()
        except Exception as exc:
            logger.warning(
                "Diagnostic agent unavailable (%s) - 'why' questions will fall back "
                "to the out-of-scope reply.", exc,
            )
            _AGENT = None
            return None

        memory.log_status(
            "diagnostic graph built in %.1fs" % (time.perf_counter() - started),
            target_logger=logger,
            baseline_mb=rss_before,
        )
        return _AGENT


def reset_diagnostic_agent() -> None:
    """Drop the memoised graph. For tests — nothing in the app should call this."""
    global _AGENT, _AGENT_ATTEMPTED
    with _AGENT_LOCK:
        _AGENT = None
        _AGENT_ATTEMPTED = False


def initial_state(
    question: str,
    metric: str,
    target: Window,
    *,
    request_id: str = "",
    comparison: Window | None = None,
    filters: list[Any] | None = None,
    max_dimensions: int = 3,
    budget: Budget | None = None,
) -> GraphState:
    """Seed the graph. `findings` starts empty because its reducer appends."""
    return {
        "question": question,
        "request_id": request_id,
        "metric": metric,
        "target": target,
        "comparison": comparison,
        "filters": filters or [],
        "max_dimensions": max_dimensions,
        "findings": [],
        "hypotheses": [],
        "budget": budget or Budget(),
        "answer": "",
        "stopped_because": "",
    }


def service_config(
    validator: Any,
    sql_generator: Any,
    *,
    driver_graph: DriverGraph | None = None,
    query_cache: Any | None = None,
    registry: Any | None = None,
    artifact_registry: Any | None = None,
    thread_id: str = "",
) -> dict:
    """Build the LangGraph config carrying the gateway services."""
    configurable: dict[str, Any] = {
        "validator": validator,
        "sql_generator": sql_generator,
        "driver_graph": driver_graph or DriverGraph.load(),
        "query_cache": query_cache,
        # Lets the planner skip a weight probe the weight metric cannot serve.
        "registry": registry,
        "artifact_registry": artifact_registry or ArtifactRegistry.load(),
    }
    if thread_id:
        configurable["thread_id"] = thread_id
    return {"configurable": configurable}
