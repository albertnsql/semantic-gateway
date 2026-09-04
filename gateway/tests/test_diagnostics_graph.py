"""
tests/test_diagnostics_graph.py — the graph runs, terminates, and degrades.

Node functions are tested directly wherever possible: they are plain callables taking
`(state, config)`, so most behaviour needs no LangGraph at all. Only the assembly
tests import it, and they skip cleanly if it is absent — which is the point of keeping
the import confined to `graph.py`.

Everything here uses fakes. Phase 2 makes zero LLM calls, so a full diagnosis is
testable end to end with nothing but a stub SQL generator.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.diagnostics.graph import (
    GraphState,
    analyze_node,
    build_diagnostic_agent,
    execute_probes_node,
    initial_state,
    plan_node,
    service_config,
    synthesize_node,
)
from core.diagnostics.playbooks import DriverGraph
from core.diagnostics.state import Budget, Finding
from core.diagnostics.windows import Window

TARGET = Window.of("2026-01-01", "2026-06-30")


class _Validation:
    safe_to_execute = True
    violations: list = []


class _Validator:
    """Accepts everything. Rejection behaviour is covered in test_diagnostics_tools."""

    def validate(self, intent):
        return _Validation()


class _SqlGenerator:
    """
    Returns rows shaped like the real warehouse: uppercase keys, one row per
    dimension value. `values` maps (metric, dimension) -> {label: amount}.
    """

    def __init__(self, values: dict, fail_on: set[str] | None = None):
        self.values = values
        self.fail_on = fail_on or set()
        self.calls: list = []

    def generate(self, intent, validation):
        self.calls.append(intent)
        return SimpleNamespace(compiled_sql="SELECT 1", metricflow_query="mf")

    def execute_query(self, sql):
        intent = self.calls[-1]
        metric = intent.metrics[0]
        if metric in self.fail_on:
            raise RuntimeError(f"{metric} unavailable")
        dim = intent.dimensions[0] if intent.dimensions else ""
        window_key = intent.time_range.start_date if intent.time_range else ""
        mapping = self.values.get((metric, dim, window_key))
        if mapping is None:
            mapping = self.values.get((metric, dim), {})
        if not dim:
            return [{metric.upper(): sum(mapping.values())}] if mapping else []
        return [{dim.upper(): label, metric.upper(): amount}
                for label, amount in mapping.items()]


def _values_for_revenue_drop():
    """
    A concrete story: revenue down, concentrated in one plan.

    target H1 2026 = 300; comparison H2 2025 = 500. premium collapsed 200 -> 20.
    """
    t = "2026-01-01"
    c = "2025-07-01"
    per_dim_target = {"basic": 100.0, "standard": 180.0, "premium": 20.0}
    per_dim_comparison = {"basic": 100.0, "standard": 180.0, "premium": 220.0}
    values = {}
    for dim in ("plan_type", "country", "payment_method", "is_renewal", "currency",
                "acquisition_channel", "age_group", "cohort_month",
                "subscription_status"):
        values[("total_revenue", dim, t)] = per_dim_target
        values[("total_revenue", dim, c)] = per_dim_comparison
    values[("total_revenue", "", t)] = per_dim_target
    values[("total_revenue", "", c)] = per_dim_comparison
    return values


@pytest.fixture(scope="module")
def graph() -> DriverGraph:
    return DriverGraph.load()


def _config(sql_generator, graph):
    return service_config(_Validator(), sql_generator, driver_graph=graph)


class TestPlanNode:
    def test_it_produces_a_plan(self, graph: DriverGraph) -> None:
        state = initial_state("why is revenue down", "total_revenue", TARGET)
        out = plan_node(state, _config(_SqlGenerator({}), graph))
        assert out["plan"] is not None
        assert out["plan"].dimensions

    def test_an_unknown_metric_stops_cleanly_instead_of_raising(
        self, graph: DriverGraph
    ) -> None:
        """A KeyError here would surface as a 500 for a question we simply can't do."""
        state = initial_state("why", "not_a_metric", TARGET)
        out = plan_node(state, _config(_SqlGenerator({}), graph))
        assert out["plan"] is None
        assert "no diagnostic playbook" in out["stopped_because"]

    def test_the_probe_budget_trims_rather_than_refuses(self, graph: DriverGraph) -> None:
        """Two dimensions of evidence beats none."""
        state = initial_state("why", "total_revenue", TARGET, budget=Budget(max_probes=4))
        out = plan_node(state, _config(_SqlGenerator({}), graph))
        assert len(out["plan"].probes) == 4
        assert any("trimmed" in n for n in out["plan"].notes)


class TestExecuteProbesNode:
    def test_every_probe_becomes_a_finding(self, graph: DriverGraph) -> None:
        state = initial_state("why", "total_revenue", TARGET, max_dimensions=2)
        state.update(plan_node(state, _config(_SqlGenerator({}), graph)))
        sql_gen = _SqlGenerator(_values_for_revenue_drop())
        out = execute_probes_node(state, _config(sql_gen, graph))
        assert len(out["findings"]) == len(state["plan"].probes)
        assert all(f.id for f in out["findings"])

    def test_a_failing_probe_does_not_stop_the_others(self, graph: DriverGraph) -> None:
        state = initial_state("why", "churn_rate", TARGET, max_dimensions=1)
        state.update(plan_node(state, _config(_SqlGenerator({}), graph)))
        sql_gen = _SqlGenerator(_values_for_revenue_drop(),
                                fail_on={"monthly_subscriber_base"})
        out = execute_probes_node(state, _config(sql_gen, graph))
        assert any(not f.ok for f in out["findings"])
        assert any(f.ok for f in out["findings"]), "one bad probe killed the round"

    def test_budget_records_what_was_spent(self, graph: DriverGraph) -> None:
        state = initial_state("why", "total_revenue", TARGET, max_dimensions=2)
        state.update(plan_node(state, _config(_SqlGenerator({}), graph)))
        out = execute_probes_node(state, _config(_SqlGenerator({}), graph))
        assert out["budget"].probes_used == len(state["plan"].probes)

    def test_no_plan_means_no_work(self, graph: DriverGraph) -> None:
        out = execute_probes_node({"plan": None}, _config(_SqlGenerator({}), graph))
        assert out == {}


class TestAnalyzeNode:
    def test_it_finds_the_concentrated_cause(self, graph: DriverGraph) -> None:
        state = initial_state("why is revenue down", "total_revenue", TARGET,
                              max_dimensions=1)
        cfg = _config(_SqlGenerator(_values_for_revenue_drop()), graph)
        state.update(plan_node(state, cfg))
        state.update(execute_probes_node(state, cfg))
        out = analyze_node(state, cfg)
        assert out["hypotheses"]
        top = out["hypotheses"][0]
        assert top.dimension == "plan_type"
        assert top.verdict == "explains"
        assert "premium" in top.statement
        assert top.evidence, "a hypothesis with no citations cannot be reported"

    def test_no_findings_yields_no_hypotheses(self, graph: DriverGraph) -> None:
        assert analyze_node({"plan": None, "findings": []}, None)["hypotheses"] == []

    def test_a_half_present_pair_is_skipped(self, graph: DriverGraph) -> None:
        """
        The comparison side failed, so there is no gap to attribute. Treating the
        missing side as zero would report the whole level as the change.
        """
        state = initial_state("why", "total_revenue", TARGET, max_dimensions=1)
        cfg = _config(_SqlGenerator(_values_for_revenue_drop()), graph)
        state.update(plan_node(state, cfg))
        state.update(execute_probes_node(state, cfg))
        dim = state["plan"].dimensions[0]
        state["findings"] = [
            f for f in state["findings"]
            if not f.label.endswith(f"by {dim}, comparison")
        ]
        out = analyze_node(state, cfg)
        assert all(h.dimension != dim for h in out["hypotheses"])


class TestSynthesizeNode:
    def test_the_answer_states_the_gap_and_the_baseline(self, graph: DriverGraph) -> None:
        state = initial_state("why is revenue down", "total_revenue", TARGET,
                              max_dimensions=1)
        cfg = _config(_SqlGenerator(_values_for_revenue_drop()), graph)
        for node in (plan_node, execute_probes_node, analyze_node):
            state.update(node(state, cfg))
        answer = synthesize_node(state, cfg)["answer"]
        assert "total_revenue is down" in answer
        assert "preceding 6 months" in answer, "an answer must name its baseline"
        assert "premium" in answer
        assert "[F" in answer, "causal claims must carry citations"

    def test_a_spread_movement_gets_the_broad_based_verdict(
        self, graph: DriverGraph
    ) -> None:
        """
        Three weak partials is honest and useless. Reproduces the live revenue shape:
        every axis moves, none by as much as half.
        """
        values = {}
        for dim in ("plan_type", "country", "payment_method", "is_renewal"):
            values[("total_revenue", dim, "2026-01-01")] = {
                "a": 143.6, "b": 130.0, "c": 126.4}
            values[("total_revenue", dim, "2025-07-01")] = {
                "a": 100.0, "b": 100.0, "c": 100.0}
        values[("total_revenue", "", "2026-01-01")] = {"t": 400.0}
        values[("total_revenue", "", "2025-07-01")] = {"t": 300.0}
        state = initial_state("why is revenue up", "total_revenue", TARGET,
                              max_dimensions=3)
        cfg = _config(_SqlGenerator(values), graph)
        for node in (plan_node, execute_probes_node, analyze_node):
            state.update(node(state, cfg))
        answer = synthesize_node(state, cfg)["answer"]
        assert "broad-based" in answer
        assert "largest single mover" in answer, (
            "the leading value on each axis is still worth naming as context"
        )
        assert "[F" in answer

    def test_a_zero_gap_is_not_broad_based(self, graph: DriverGraph) -> None:
        """
        "Nothing happened" and "it moved everywhere" are opposite findings.

        `concentration()` returns a top share of 0.0 when the gap is ~0, which is
        below any threshold — so without the inconclusive guard in `is_broad_based`
        an unmoved metric is reported as broad-based. This regressed exactly once.
        """
        flat = {}
        for dim in ("plan_type", "country", "payment_method"):
            flat[("total_revenue", dim, "2026-01-01")] = {"a": 50.0, "b": 50.0}
            flat[("total_revenue", dim, "2025-07-01")] = {"a": 50.0, "b": 50.0}
        flat[("total_revenue", "", "2026-01-01")] = {"a": 50.0, "b": 50.0}
        flat[("total_revenue", "", "2025-07-01")] = {"a": 50.0, "b": 50.0}
        state = initial_state("why", "total_revenue", TARGET, max_dimensions=2)
        cfg = _config(_SqlGenerator(flat), graph)
        for node in (plan_node, execute_probes_node, analyze_node):
            state.update(node(state, cfg))
        answer = synthesize_node(state, cfg)["answer"]
        assert "broad-based" not in answer

    def test_it_says_so_when_nothing_explains_the_gap(self, graph: DriverGraph) -> None:
        """A confident 'I checked and none of it explains this' is a good answer."""
        flat = {}
        for dim in ("plan_type", "country", "payment_method"):
            flat[("total_revenue", dim, "2026-01-01")] = {"a": 50.0, "b": 50.0}
            flat[("total_revenue", dim, "2025-07-01")] = {"a": 50.0, "b": 50.0}
        flat[("total_revenue", "", "2026-01-01")] = {"a": 50.0, "b": 50.0}
        flat[("total_revenue", "", "2025-07-01")] = {"a": 50.0, "b": 50.0}
        state = initial_state("why", "total_revenue", TARGET, max_dimensions=2)
        cfg = _config(_SqlGenerator(flat), graph)
        for node in (plan_node, execute_probes_node, analyze_node):
            state.update(node(state, cfg))
        answer = synthesize_node(state, cfg)["answer"]
        assert "No single factor explains it" in answer

    def test_cautions_reach_the_answer(self, graph: DriverGraph) -> None:
        """Metric-level cautions must reach the answer even when analysis finds nothing."""
        state = initial_state("why is churn up", "churn_rate", TARGET, max_dimensions=1)
        cfg = _config(_SqlGenerator({}), graph)
        for node in (plan_node, execute_probes_node, analyze_node):
            state.update(node(state, cfg))
        answer = synthesize_node(state, cfg)["answer"]
        assert "Caveat:" in answer and "base SHRANK" in answer

    def test_a_window_scoped_data_warning_reaches_the_answer(
        self, graph: DriverGraph
    ) -> None:
        """
        A window reaching the end of fct_mrr_monthly must disclose the phantom
        period: on 2026-08-28 the newest period held 531 rows of which all 531 were
        churned, a 100% rate against no active subscribers.

        This used the June 2026 bad append until 2026-08-28, when checking the
        warehouse showed the repairs had landed and that artifact was retired. The
        mechanism is the same; only the live example changed.
        """
        from datetime import date

        from core.diagnostics.windows import month_start
        # Anchor on today so the relative window resolves, whenever this runs.
        current = Window(month_start(date.today()), month_start(date.today()))
        state = initial_state("why is churn up", "churn_rate",
                              current.align_to_months(), max_dimensions=1)
        cfg = _config(_SqlGenerator({}), graph)
        for node in (plan_node, execute_probes_node, analyze_node):
            state.update(node(state, cfg))
        answer = synthesize_node(state, cfg)["answer"]
        assert "Data warning:" in answer
        assert "date spine" in answer

    def test_a_retired_artifact_no_longer_reaches_the_answer(
        self, graph: DriverGraph
    ) -> None:
        """
        June 2026's bad append was verified repaired on 2026-08-28. Continuing to warn
        would tell a reader that a genuine churn increase might be fake.
        """
        june = Window.of("2026-06-01", "2026-06-30")
        state = initial_state("why is churn up", "churn_rate", june, max_dimensions=1)
        cfg = _config(_SqlGenerator({}), graph)
        for node in (plan_node, execute_probes_node, analyze_node):
            state.update(node(state, cfg))
        answer = synthesize_node(state, cfg)["answer"]
        assert "bad monthly append" not in answer

    def test_an_inapplicable_data_warning_stays_out(self, graph: DriverGraph) -> None:
        """
        The other half of the same live bug: the trailing churn-only warning fired on
        a June window while August was the newest period.
        """
        june = Window.of("2026-06-01", "2026-06-30")
        state = initial_state("why is churn up", "churn_rate", june, max_dimensions=1)
        cfg = _config(_SqlGenerator({}), graph)
        for node in (plan_node, execute_probes_node, analyze_node):
            state.update(node(state, cfg))
        answer = synthesize_node(state, cfg)["answer"]
        assert "churn-only" not in answer, (
            "a warning about the newest period fired on a window that does not reach it"
        )

    def test_an_unplannable_question_gets_a_plain_explanation(self) -> None:
        out = synthesize_node(
            {"plan": None, "stopped_because": "no diagnostic playbook exists for 'x'"},
            None,
        )
        assert "no diagnostic playbook" in out["answer"]

    def test_early_stop_is_disclosed(self, graph: DriverGraph) -> None:
        state = initial_state("why", "total_revenue", TARGET,
                              budget=Budget(max_probes=4))
        cfg = _config(_SqlGenerator(_values_for_revenue_drop()), graph)
        for node in (plan_node, execute_probes_node, analyze_node):
            state.update(node(state, cfg))
        state["budget"].probes_used = 99
        answer = synthesize_node(state, cfg)["answer"]
        assert "Stopped early" in answer


class TestCompiledGraph:
    """The only tests that need langgraph installed."""

    def test_it_compiles(self) -> None:
        pytest.importorskip("langgraph")
        assert build_diagnostic_agent() is not None

    def test_a_full_diagnosis_runs_end_to_end(self, graph: DriverGraph) -> None:
        pytest.importorskip("langgraph")
        agent = build_diagnostic_agent()
        sql_gen = _SqlGenerator(_values_for_revenue_drop())
        final = agent.invoke(
            initial_state("why is revenue down", "total_revenue", TARGET,
                          max_dimensions=1),
            _config(sql_gen, graph),
        )
        assert final["answer"]
        assert "premium" in final["answer"]
        assert final["hypotheses"] and final["findings"]

    def test_it_terminates_without_a_reflect_loop(self, graph: DriverGraph) -> None:
        """
        Phase 2 is linear. A recursion_limit of 5 is more than the four nodes need,
        so hitting it would mean an accidental cycle.
        """
        pytest.importorskip("langgraph")
        agent = build_diagnostic_agent()
        final = agent.invoke(
            initial_state("why", "total_revenue", TARGET, max_dimensions=1),
            {**_config(_SqlGenerator(_values_for_revenue_drop()), graph),
             "recursion_limit": 5},
        )
        assert final["answer"]

    def test_findings_accumulate_through_the_reducer(self, graph: DriverGraph) -> None:
        """
        `findings` carries operator.add so Phase 3 can fan out concurrently without
        LangGraph rejecting concurrent writes.
        """
        pytest.importorskip("langgraph")
        agent = build_diagnostic_agent()
        final = agent.invoke(
            initial_state("why", "total_revenue", TARGET, max_dimensions=2),
            _config(_SqlGenerator(_values_for_revenue_drop()), graph),
        )
        # 2 baseline + 1 trend + 2 per dimension. The trend probe is what lets the
        # answer say whether the comparison window was itself typical.
        assert len(final["findings"]) == 3 + 2 * 2

    def test_zero_llm_calls(self, graph: DriverGraph, monkeypatch) -> None:
        """
        Phase 2's headline property. The binding constraint on this feature is a
        15 req/min LLM quota with no working fallback, so a diagnosis that costs
        nothing against it does not compete with chat traffic.
        """
        pytest.importorskip("langgraph")
        import openai

        def _explode(*args, **kwargs):
            raise AssertionError("the diagnostic graph made an LLM call")

        monkeypatch.setattr(openai.OpenAI, "__init__", _explode)
        agent = build_diagnostic_agent()
        final = agent.invoke(
            initial_state("why", "total_revenue", TARGET, max_dimensions=1),
            _config(_SqlGenerator(_values_for_revenue_drop()), graph),
        )
        assert final["answer"]
