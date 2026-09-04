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
    baseline_representativeness,
    build_hypothesis,
    decompose,
    is_broad_based,
    rank_hypotheses,
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
    stopped_because: str


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
    try:
        plan = plan_time_comparison(
            metric,
            state["target"],
            graph=graph,
            comparison=state.get("comparison"),
            max_dimensions=state.get("max_dimensions", 3),
            filters=state.get("filters") or [],
            weight_available=_weight_availability(_services(config)),
        )
    except KeyError as exc:
        logger.info("no playbook for %s: %s", metric, exc)
        return {
            "plan": None,
            "stopped_because": f"no diagnostic playbook exists for '{metric}'",
        }

    budget = state.get("budget") or Budget()
    if len(plan.probes) > budget.probes_remaining:
        # Trim rather than refuse: a two-dimension diagnosis is worth more than none.
        keep = plan.probes[: budget.probes_remaining]
        logger.info(
            "probe budget trims the plan from %d to %d", len(plan.probes), len(keep)
        )
        plan = ProbePlan(
            metric=plan.metric, target=plan.target, comparison=plan.comparison,
            probes=keep, dimensions=plan.dimensions, cautions=plan.cautions,
            weight_metric=plan.weight_metric,
            # `trend` must be carried, or a trimmed plan silently loses the baseline
            # representativeness check and the answer stops warning about an
            # unrepresentative comparison without saying why.
            trend=plan.trend,
            notes=plan.notes + ["the probe budget trimmed this plan"],
        )

    logger.info(
        "planned %d probe(s) for %s over %s vs %s",
        len(plan.probes), metric, plan.target, plan.comparison,
    )
    return {"plan": plan}


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
                existing=findings, label=probe.label, query_cache=query_cache,
            )
        except ProbeRejected as exc:
            # Governance said no. That is a driver-graph bug, not a data problem, so
            # record it as a failed finding and keep going rather than aborting.
            logger.warning("probe rejected: %s", exc)
            finding = Finding(
                id=f"F{len(findings) + 1}", label=probe.label, metric=probe.metric,
                dimensions=probe.dimensions, rows=[], error=str(exc),
            )
        findings.append(finding)
        budget.probes_used += 1
        if started is None:
            started = time.perf_counter()   # the engine is warm from here on
        else:
            budget.seconds_used = time.perf_counter() - started

    ok = sum(1 for f in findings if f.ok)
    logger.info(
        "executed %d probe(s), %d ok, in %.0f ms after warm-up",
        len(findings), ok, budget.seconds_used * 1000,
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
        hypotheses.append(build_hypothesis(result, evidence=evidence))

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
        return {"answer": stopped or "This question could not be diagnosed."}

    lines: list[str] = []
    baseline = _baseline_gap(plan, state.get("findings") or [])
    comparison_phrase = describe_comparison(plan.target, plan.comparison)
    if baseline is not None:
        target_total, comparison_total, gap = baseline
        direction = "down" if gap < 0 else "up"
        pct = (gap / comparison_total * 100) if comparison_total else 0.0
        lines.append(
            f"{plan.metric} is {direction} {abs(pct):.1f}% against {comparison_phrase} "
            f"({target_total:,.2f} vs {comparison_total:,.2f})."
        )
    else:
        lines.append(
            f"Comparing {plan.metric} against {comparison_phrase}; the baseline "
            "totals could not be established."
        )

    # A gap is only as meaningful as what it is measured against. A live June churn
    # diagnosis reported +163.7% against a May that was itself 41% below the trailing
    # 12-month average; against trend the figure is +55%. Both correct, one useful.
    # This says so rather than swapping the baseline for another guessable one.
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

    explaining = [h for h in hypotheses if h.verdict in ("explains", "partial")]
    ruled_out = [h for h in hypotheses if h.verdict == "not_it"]

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
        for h in explaining:
            lines.append(f"{h.statement} [{', '.join(h.evidence)}]")
    else:
        checked = ", ".join(h.dimension for h in hypotheses) or "no dimension"
        lines.append(
            f"No single factor explains it. I checked {checked} and none accounts "
            "for a material share of the gap."
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

    return {"answer": "\n".join(lines), "stopped_because": stopped}


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

    Linear in Phase 2: plan -> execute -> analyze -> synthesize. The reflect loop is
    Phase 4, and the conditional edge it needs goes between analyze and synthesize.
    Deliberately not added yet: a loop with nothing to iterate on is untestable
    scaffolding.
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
    builder.add_edge("analyze", "synthesize")
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
