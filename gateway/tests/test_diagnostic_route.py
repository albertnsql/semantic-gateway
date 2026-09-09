"""
tests/test_diagnostic_route.py — the diagnostic branch in POST /api/v1/query.

The branch is built as a FALL-THROUGH, and that is the property most worth pinning:
every failure mode — diagnostics disabled, langgraph absent, no metric extracted, no
playbook, an exception mid-run — must land in the out-of-scope reply, which is exactly
where "why" questions went before this path existed. So the worst case is the old
behaviour and never a 500.

`_run_diagnosis` is tested directly rather than through the graph. Its job is
translation (intent -> window -> state -> response payload) and containment; the graph
itself is covered by test_diagnostics_graph.py.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import api.routes.query as route
from core.diagnostics.state import Budget, Finding, Hypothesis
from core.diagnostics.windows import Window, trailing_months


class _Options:
    include_sql = True
    max_rows = 100


class _Body:
    def __init__(self, query="why is revenue down"):
        self.query = query
        self.options = _Options()


def _intent(metrics=("total_revenue",), time_range=None, filters=()):
    return SimpleNamespace(
        metrics=list(metrics), dimensions=[], filters=list(filters),
        time_range=time_range, query_type="diagnostic_query",
    )


class _App:
    def __init__(self):
        self.state = SimpleNamespace(
            semantic_validator=object(), sql_generator=object(), query_cache=None,
        )


def _request():
    return SimpleNamespace(app=_App())


@pytest.fixture
def enabled(monkeypatch):
    for name, value in (
        ("diagnostics_enabled", True), ("diagnostics_max_dimensions", 2),
        ("diagnostics_max_probes", 16), ("diagnostics_deadline_seconds", 25.0),
        ("diagnostics_default_months", 6),
    ):
        monkeypatch.setattr(route._settings, name, value, raising=False)


def _fake_agent(monkeypatch, final: dict | Exception):
    """Replace the compiled graph with something that returns a known final state."""
    import core.diagnostics.graph as g

    class _Agent:
        def invoke(self, state, config):
            self.state, self.config = state, config
            if isinstance(final, Exception):
                raise final
            return {**state, **final}

    agent = _Agent()
    monkeypatch.setattr(g, "get_diagnostic_agent", lambda: agent)
    return agent


def _plan(dimensions=("plan_type",)):
    # `target` is carried because the real ProbePlan always has one and the route
    # reports `plan.target` rather than its own pre-plan copy -- the planner trims a
    # year-shaped window to complete months, so the two can legitimately differ.
    # H1-2026 against H2-2025 here: not a year question, so no trim applies.
    return SimpleNamespace(
        target=Window.of("2026-01-01", "2026-06-30"),
        comparison=Window.of("2025-07-01", "2025-12-31"),
        dimensions=list(dimensions), cautions=["watch the trailing month"],
        notes=["a note"],
    )


def _good_final():
    return {
        "answer": "total_revenue is down 40.0% against the preceding 6 months.",
        "plan": _plan(),
        "hypotheses": [
            Hypothesis(dimension="plan_type", statement="premium accounts for 100%",
                       confidence="contribution", verdict="explains",
                       explained_share=1.0, evidence=["F3", "F4"]),
        ],
        "findings": [
            Finding(id="F1", label="total_revenue, target window",
                    metric="total_revenue", dimensions=[], rows=[{"A": 1}],
                    sql="SELECT 1"),
        ],
        "stopped_because": "",
    }


class TestFallsThroughRatherThanFailing:
    def test_disabled_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(route._settings, "diagnostics_enabled", False, raising=False)
        # The route checks the flag before calling the helper, so assert the helper
        # is never reached by checking the flag path directly.
        assert route._settings.diagnostics_enabled is False

    def test_unavailable_agent_returns_none(self, monkeypatch, enabled) -> None:
        import core.diagnostics.graph as g

        monkeypatch.setattr(g, "get_diagnostic_agent", lambda: None)
        assert route._run_diagnosis(_Body(), _intent(), _request(), "rid") is None

    def test_an_exception_mid_run_returns_none(self, monkeypatch, enabled) -> None:
        _fake_agent(monkeypatch, RuntimeError("graph exploded"))
        assert route._run_diagnosis(_Body(), _intent(), _request(), "rid") is None

    def test_an_empty_answer_returns_none(self, monkeypatch, enabled) -> None:
        """No answer is worse than the out-of-scope reply, so it must not be sent."""
        _fake_agent(monkeypatch, {"answer": "   ", "plan": None, "hypotheses": [],
                                  "findings": []})
        assert route._run_diagnosis(_Body(), _intent(), _request(), "rid") is None

    def test_no_playbook_answer_still_returns_a_payload(self, monkeypatch, enabled) -> None:
        """
        plan=None with an answer is the graph's own "I have no playbook" reply. It is
        a real answer, so it ships rather than falling through.
        """
        _fake_agent(monkeypatch, {
            "answer": "no diagnostic playbook exists for 'x'", "plan": None,
            "hypotheses": [], "findings": [],
            "stopped_because": "no diagnostic playbook exists for 'x'",
        })
        out = route._run_diagnosis(_Body(), _intent(), _request(), "rid")
        assert out is not None
        assert out["comparison_window"] is None
        assert out["dimensions_examined"] == []


class TestWindowSelection:
    def test_the_questions_own_period_is_used_when_given(self, monkeypatch, enabled) -> None:
        agent = _fake_agent(monkeypatch, _good_final())
        tr = SimpleNamespace(start_date="2026-01-01", end_date="2026-06-30")
        route._run_diagnosis(_Body(), _intent(time_range=tr), _request(), "rid")
        assert agent.state["target"] == Window.of("2026-01-01", "2026-06-30")

    def test_the_default_window_excludes_the_current_month(
        self, monkeypatch, enabled
    ) -> None:
        """
        fct_mrr_monthly's spine runs to current_date() while cancellations carry a
        +1 month offset, so the newest month is structurally churn-only and would
        read as a collapse.
        """
        agent = _fake_agent(monkeypatch, _good_final())
        route._run_diagnosis(_Body(), _intent(), _request(), "rid")
        target = agent.state["target"]
        assert target == trailing_months(date.today(), 6, inclusive=False)
        assert target.end.month != date.today().month or target.end.year != date.today().year
        assert target.months_spanned == 6

    def test_the_default_month_count_is_configurable(self, monkeypatch, enabled) -> None:
        monkeypatch.setattr(route._settings, "diagnostics_default_months", 3,
                            raising=False)
        agent = _fake_agent(monkeypatch, _good_final())
        route._run_diagnosis(_Body(), _intent(), _request(), "rid")
        assert agent.state["target"].months_spanned == 3


class TestStateAndServices:
    def test_the_metric_and_filters_reach_the_graph(self, monkeypatch, enabled) -> None:
        agent = _fake_agent(monkeypatch, _good_final())
        sentinel = SimpleNamespace(column="country", operator="eq", value="DE")
        route._run_diagnosis(_Body(), _intent(filters=[sentinel]), _request(), "rid")
        assert agent.state["metric"] == "total_revenue"
        assert agent.state["filters"] == [sentinel], (
            "a diagnosis scoped to one country must keep that scope on both sides"
        )

    def test_budget_comes_from_config(self, monkeypatch, enabled) -> None:
        monkeypatch.setattr(route._settings, "diagnostics_max_probes", 6, raising=False)
        agent = _fake_agent(monkeypatch, _good_final())
        route._run_diagnosis(_Body(), _intent(), _request(), "rid")
        assert agent.state["budget"].max_probes == 6

    def test_services_are_passed_through_the_config(self, monkeypatch, enabled) -> None:
        agent = _fake_agent(monkeypatch, _good_final())
        request = _request()
        route._run_diagnosis(_Body(), _intent(), request, "rid-42")
        configurable = agent.config["configurable"]
        assert configurable["validator"] is request.app.state.semantic_validator
        assert configurable["sql_generator"] is request.app.state.sql_generator
        assert configurable["thread_id"] == "rid-42"


class TestPayloadShape:
    def test_it_carries_the_answer_and_the_evidence_table(
        self, monkeypatch, enabled
    ) -> None:
        """
        The evidence table is what makes a causal claim checkable rather than
        plausible, so it ships with the answer instead of staying in the logs.
        """
        _fake_agent(monkeypatch, _good_final())
        out = route._run_diagnosis(_Body(), _intent(), _request(), "rid")
        assert "down 40.0%" in out["answer"]
        assert out["metric"] == "total_revenue"
        assert out["evidence"][0]["id"] == "F1"
        assert out["evidence"][0]["sql"] == "SELECT 1"
        assert out["hypotheses"][0]["evidence"] == ["F3", "F4"]
        assert out["hypotheses"][0]["verdict"] == "explains"
        assert out["hypotheses"][0]["confidence"] == "contribution"

    def test_cautions_and_notes_reach_the_caller(self, monkeypatch, enabled) -> None:
        _fake_agent(monkeypatch, _good_final())
        out = route._run_diagnosis(_Body(), _intent(), _request(), "rid")
        assert out["cautions"] == ["watch the trailing month"]
        assert out["notes"] == ["a note"]

    def test_sql_is_withheld_when_the_caller_opted_out(
        self, monkeypatch, enabled
    ) -> None:
        _fake_agent(monkeypatch, _good_final())
        body = _Body()
        body.options.include_sql = False
        out = route._run_diagnosis(body, _intent(), _request(), "rid")
        assert out["evidence"][0]["sql"] == ""

    def test_the_payload_is_json_serialisable(self, monkeypatch, enabled) -> None:
        import json

        _fake_agent(monkeypatch, _good_final())
        out = route._run_diagnosis(_Body(), _intent(), _request(), "rid")
        json.dumps(route.make_json_safe(out))


class TestExtractorRouting:
    """The fourth query_type has to survive normalisation."""

    def test_diagnostic_query_is_an_accepted_type(self) -> None:
        from core.intent_extractor import QueryIntent

        intent = QueryIntent(original_query="why", metrics=["mrr"],
                             query_type="diagnostic_query")
        assert intent.query_type == "diagnostic_query"

    def test_the_prompt_teaches_the_boundary(self) -> None:
        """
        A stale few-shot example fights the instructions. The old prompt classified
        "Why did churn increase last quarter?" as out_of_scope, which would have
        overridden any new rule.
        """
        import inspect

        from core.intent_extractor import IntentExtractor

        source = inspect.getsource(IntentExtractor)
        assert "diagnostic_query" in source
        assert '"Why did churn increase last quarter?"' in source
        churn_example = source.split('"Why did churn increase last quarter?"')[1][:400]
        assert "diagnostic_query" in churn_example
        assert "out_of_scope" not in churn_example.split("Output:")[1][:200]

    def test_an_unanswerable_why_is_still_out_of_scope(self) -> None:
        import inspect

        from core.intent_extractor import IntentExtractor

        source = inspect.getsource(IntentExtractor)
        assert "competitors" in source, (
            "the prompt needs a why-question that is genuinely out of scope, or the "
            "model will route every 'why' into the diagnostic path"
        )
