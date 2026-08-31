"""
tests/test_diagnostics_playbooks.py — the planner only asks for things that exist.

`playbooks.py` is the boundary between "what the driver graph declares" and "what the
agent actually queries". Two failure modes matter:

* asking for something uncertified — costs a probe slot mid-run and leaves the
  diagnosis with a hole in its evidence
* asking for only one side of a comparison — produces a level described as a change,
  which reads as a finding and is not one

The plan is deterministic, so these assert exact probe sets rather than properties.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.diagnostics.playbooks import (
    DriverGraph,
    ProbePlan,
    pair_findings,
    plan_time_comparison,
    previous_window,
    usable_pairs,
)
from core.diagnostics.state import Finding
from core.diagnostics.windows import Window


@pytest.fixture(scope="module")
def graph() -> DriverGraph:
    return DriverGraph.load()


TARGET = Window.of("2026-01-01", "2026-06-30")


class TestDriverGraphAccessor:
    def test_it_loads_the_shipped_file(self, graph: DriverGraph) -> None:
        assert graph.knows("total_revenue")
        assert graph.decompose_by("total_revenue")

    def test_unknown_metric_is_not_silently_empty(self, graph: DriverGraph) -> None:
        assert not graph.knows("no_such_metric")
        assert graph.decompose_by("no_such_metric") == []

    def test_preferred_dimensions_come_first(self, graph: DriverGraph) -> None:
        """
        The order encodes a prior about which slices explain a movement here.
        Alphabetical would put acquisition_channel ahead of plan_type for no reason.
        """
        ordered = graph.ordered_dimensions("total_revenue")
        assert ordered[0] == "plan_type"
        assert ordered.index("country") < ordered.index("payment_method")

    def test_ordering_never_invents_a_dimension(self, graph: DriverGraph) -> None:
        for metric in ("total_revenue", "churn_rate", "engagement_rate"):
            assert set(graph.ordered_dimensions(metric)) <= set(graph.decompose_by(metric))

    def test_limit_truncates(self, graph: DriverGraph) -> None:
        assert len(graph.ordered_dimensions("total_revenue", limit=2)) == 2

    def test_weight_metric_is_exposed(self, graph: DriverGraph) -> None:
        assert graph.weight_metric("churn_rate") == "monthly_subscriber_base"
        assert graph.weight_metric("total_revenue") == ""


class TestPlanShape:
    def test_unknown_metric_raises_rather_than_planning_nothing(
        self, graph: DriverGraph
    ) -> None:
        """An empty plan would read as 'nothing to investigate'."""
        with pytest.raises(KeyError, match="no driver_graph entry"):
            plan_time_comparison("not_a_metric", TARGET, graph=graph)

    def test_baseline_pair_is_always_planned_first(self, graph: DriverGraph) -> None:
        """
        Without it, every share is computed against a total nobody measured.
        """
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph)
        assert plan.probes[0].role == "baseline"
        assert plan.probes[1].role == "comparison"
        assert plan.probes[0].dimensions == [] and plan.probes[1].dimensions == []

    def test_every_dimension_gets_both_sides(self, graph: DriverGraph) -> None:
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    max_dimensions=3, include_weights=False)
        for dim in plan.dimensions:
            roles = {p.role for p in plan.probes if p.dimension == dim}
            assert roles == {"target", "comparison"}, f"{dim} is missing a side"

    def test_probe_count_without_weights(self, graph: DriverGraph) -> None:
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    max_dimensions=3, include_weights=False)
        assert len(plan.probes) == 2 + 2 * 3

    def test_probe_count_with_weights(self, graph: DriverGraph) -> None:
        """A weighted metric needs its denominator on both sides too."""
        plan = plan_time_comparison("churn_rate", TARGET, graph=graph, max_dimensions=3)
        assert len(plan.probes) == 2 + 4 * 3
        weights = [p for p in plan.probes if p.role.startswith("weight_")]
        assert {p.metric for p in weights} == {"monthly_subscriber_base"}

    def test_a_metric_without_weights_says_so(self, graph: DriverGraph) -> None:
        """
        total_revenue is additive, so mix and rate cannot be separated. The plan
        records that rather than leaving the caller to wonder.
        """
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph)
        assert plan.weight_metric == ""
        assert any("only the additive" in n for n in plan.notes)

    def test_cautions_travel_with_the_plan(self, graph: DriverGraph) -> None:
        """
        Cautions must reach the answer even when analysis finds nothing.

        These are METRIC-level traps that hold regardless of window. The
        window-sensitive ones moved to known_artifacts.yml after the trailing
        churn-only warning fired on a June diagnosis in August.
        """
        plan = plan_time_comparison("churn_rate", TARGET, graph=graph)
        assert plan.cautions
        assert any("base SHRANK" in c for c in plan.cautions)
        assert not any("churn-only" in c for c in plan.cautions), (
            "window-sensitive traps belong in known_artifacts.yml, not here"
        )

    def test_labels_are_unique_so_findings_can_be_paired(self, graph: DriverGraph) -> None:
        plan = plan_time_comparison("churn_rate", TARGET, graph=graph, max_dimensions=3)
        labels = [p.label for p in plan.probes]
        assert len(labels) == len(set(labels))


class TestComparisonWindow:
    def test_default_comparison_is_the_preceding_period(self, graph: DriverGraph) -> None:
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph)
        assert plan.comparison == Window.of("2025-07-01", "2025-12-31")

    def test_default_comparison_never_overlaps_the_target(self, graph: DriverGraph) -> None:
        """The trap windows.py exists for, asserted at the planning boundary too."""
        ragged = Window.of("2026-05-20", "2026-08-20")
        plan = plan_time_comparison("mrr", ragged, graph=graph)
        assert not plan.comparison.overlaps(ragged.align_to_months())

    def test_an_explicit_comparison_is_honoured(self, graph: DriverGraph) -> None:
        yoy = Window.of("2025-01-01", "2025-06-30")
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph, comparison=yoy)
        assert plan.comparison == yoy

    def test_target_and_comparison_windows_land_on_the_right_probes(
        self, graph: DriverGraph
    ) -> None:
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    max_dimensions=1, include_weights=False)
        for probe in plan.probes:
            expected = plan.target if probe.role in ("baseline", "target") else plan.comparison
            assert probe.window == expected

    def test_previous_window_helper_matches_the_planner(self) -> None:
        assert previous_window(TARGET) == Window.of("2025-07-01", "2025-12-31")


class TestFiltersScopeBothSides:
    def test_a_filter_is_carried_onto_every_probe(self, graph: DriverGraph) -> None:
        """
        A diagnosis scoped to one country must keep that scope on the comparison
        side. Otherwise it compares a segment against the whole population and
        attributes the difference to time.
        """
        sentinel = object()
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    filters=[sentinel], max_dimensions=2)
        assert plan.probes and all(sentinel in p.filters for p in plan.probes)

    def test_a_pinned_dimension_is_not_used_as_an_axis(self, graph: DriverGraph) -> None:
        """
        Decomposing by a dimension the filter pins yields one bucket holding 100% of
        the gap — arithmetic tautology that reads as a finding. Observed live on
        "why is revenue lower in Germany": `country = DE accounts for 100.0%`.
        """
        pinned = SimpleNamespace(column="country", operator="eq", value="DE")
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    filters=[pinned], max_dimensions=3)
        assert "country" not in plan.dimensions
        assert len(plan.dimensions) == 3, "a pinned axis should be replaced, not lost"

    def test_a_qualified_filter_column_still_pins(self, graph: DriverGraph) -> None:
        pinned = SimpleNamespace(column="subscriber__country", operator="eq", value="DE")
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    filters=[pinned])
        assert "country" not in plan.dimensions

    def test_a_multi_value_filter_does_not_pin(self, graph: DriverGraph) -> None:
        """"revenue for US and DE" still has something to decompose by country."""
        wide = SimpleNamespace(column="country", operator="in", value=["US", "DE"])
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph, filters=[wide])
        assert "country" in plan.dimensions

    def test_a_single_element_in_filter_does_pin(self, graph: DriverGraph) -> None:
        one = SimpleNamespace(column="country", operator="in", value=["DE"])
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph, filters=[one])
        assert "country" not in plan.dimensions

    def test_an_inequality_does_not_pin(self, graph: DriverGraph) -> None:
        gt = SimpleNamespace(column="country", operator="gt", value="M")
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph, filters=[gt])
        assert "country" in plan.dimensions

    def test_filters_are_copied_not_shared(self, graph: DriverGraph) -> None:
        """A probe mutating its filter list must not affect the others."""
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph, filters=["x"])
        plan.probes[0].filters.append("mutated")
        assert "mutated" not in plan.probes[1].filters


def _finding(label: str, ok: bool = True) -> Finding:
    return Finding(id="F", label=label, metric="m", dimensions=[],
                   rows=[{"A": 1}] if ok else [], error="" if ok else "boom")


class TestPairing:
    def test_findings_group_by_dimension_and_role(self, graph: DriverGraph) -> None:
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    max_dimensions=2, include_weights=False)
        grouped = pair_findings(plan, [_finding(p.label) for p in plan.probes])
        assert "" in grouped and set(grouped[""]) == {"baseline", "comparison"}
        for dim in plan.dimensions:
            assert set(grouped[dim]) == {"target", "comparison"}

    def test_a_failed_probe_is_dropped_not_paired(self, graph: DriverGraph) -> None:
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    max_dimensions=2, include_weights=False)
        first_dim = plan.dimensions[0]
        findings = [
            _finding(p.label, ok=not (p.dimension == first_dim and p.role == "comparison"))
            for p in plan.probes
        ]
        grouped = pair_findings(plan, findings)
        assert "comparison" not in grouped.get(first_dim, {})
        assert first_dim not in usable_pairs(grouped), (
            "a half-present pair must not be decomposed - the missing side would be "
            "treated as zero and the whole level reported as the change"
        )

    def test_usable_pairs_excludes_the_baseline(self, graph: DriverGraph) -> None:
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    max_dimensions=2, include_weights=False)
        grouped = pair_findings(plan, [_finding(p.label) for p in plan.probes])
        assert "" not in usable_pairs(grouped)
        assert set(usable_pairs(grouped)) == set(plan.dimensions)

    def test_unknown_labels_are_ignored(self, graph: DriverGraph) -> None:
        plan = plan_time_comparison("total_revenue", TARGET, graph=graph,
                                    max_dimensions=1, include_weights=False)
        grouped = pair_findings(plan, [_finding("something else entirely")])
        assert usable_pairs(grouped) == []


class TestPlanIsDeterministic:
    def test_two_identical_calls_produce_identical_plans(self, graph: DriverGraph) -> None:
        """
        The reason Phase 2 plans without an LLM: reproducibility. The same question
        must yield the same probes, or a diagnosis cannot be re-run or cached.
        """
        a = plan_time_comparison("churn_rate", TARGET, graph=graph)
        b = plan_time_comparison("churn_rate", TARGET, graph=graph)
        assert [p.label for p in a.probes] == [p.label for p in b.probes]
        assert a.dimensions == b.dimensions and a.comparison == b.comparison

    def test_no_probe_asks_for_an_uncertified_dimension(self, graph: DriverGraph) -> None:
        """
        Every planned dimension must come from decompose_by, which
        test_driver_graph.py has already verified compiles AND is populated.
        """
        for metric in ("total_revenue", "churn_rate", "mrr", "engagement_rate", "ltv"):
            plan = plan_time_comparison(metric, TARGET, graph=graph)
            allowed = set(graph.decompose_by(metric))
            for probe in plan.probes:
                assert set(probe.dimensions) <= allowed, f"{metric}: {probe.dimensions}"
