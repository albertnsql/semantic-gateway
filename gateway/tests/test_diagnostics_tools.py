"""
tests/test_diagnostics_tools.py — governance is actually enforced on probes.

`run_governed_query()` is the agent's only route to the warehouse. The claim the
whole design rests on is that a probe is an ordinary governed query — so these tests
assert the seam behaves, using fakes rather than a live warehouse.

Two behaviours matter more than the rest:

* a rejected probe RAISES. It means the planner asked for something uncertified,
  which is a driver-graph bug to fix, not an empty result to absorb.
* a failed probe does NOT raise. One dead probe should cost its own evidence slot,
  not the diagnosis.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.diagnostics.state import Finding
from core.diagnostics.tools import ProbeRejected, build_probe_intent, run_governed_query


class _Validation:
    def __init__(self, safe: bool, violations=()):
        self.safe_to_execute = safe
        self.violations = [SimpleNamespace(message=m) for m in violations]


class _Validator:
    def __init__(self, safe: bool = True, violations=()):
        self._result = _Validation(safe, violations)
        self.calls: list = []

    def validate(self, intent):
        self.calls.append(intent)
        return self._result


class _SqlGenerator:
    def __init__(self, rows=None, raises: Exception | None = None):
        self._rows = rows if rows is not None else [{"PLAN_TYPE": "basic", "MRR": 1.0}]
        self._raises = raises
        self.generated: list = []
        self.executed: list[str] = []

    def generate(self, intent, validation):
        self.generated.append(intent)
        return SimpleNamespace(compiled_sql="SELECT 1", metricflow_query="mf query")

    def execute_query(self, sql):
        self.executed.append(sql)
        if self._raises:
            raise self._raises
        return self._rows


def _intent(metric="mrr", dims=("subscription__plan_type",)):
    return build_probe_intent(metric, dims)


class TestProbeIntent:
    def test_it_is_always_a_metric_query(self) -> None:
        """
        A probe must never be re-routed as a schema question, or — worse — back into
        the diagnostic path, which would recurse.
        """
        assert _intent().query_type == "metric_query"

    def test_metric_and_dimensions_are_carried(self) -> None:
        intent = build_probe_intent("total_revenue", ["subscriber__country"])
        assert intent.metrics == ["total_revenue"]
        assert intent.dimensions == ["subscriber__country"]

    def test_it_describes_itself_when_no_query_is_given(self) -> None:
        assert "total_revenue" in build_probe_intent("total_revenue", ["x"]).original_query


class TestGovernanceIsEnforced:
    def test_validator_runs_before_any_sql_is_generated(self) -> None:
        validator, sql_gen = _Validator(safe=False, violations=["nope"]), _SqlGenerator()
        with pytest.raises(ProbeRejected):
            run_governed_query(_intent(), validator=validator, sql_generator=sql_gen)
        assert validator.calls, "validator was not consulted"
        assert not sql_gen.generated, "SQL was generated for a rejected probe"
        assert not sql_gen.executed

    def test_rejection_carries_the_violations(self) -> None:
        validator = _Validator(safe=False, violations=["dim not certified", "grain"])
        with pytest.raises(ProbeRejected) as exc:
            run_governed_query(_intent(), validator=validator,
                               sql_generator=_SqlGenerator())
        assert exc.value.violations == ["dim not certified", "grain"]
        assert "not certified" in str(exc.value)


class TestSuccessfulProbe:
    def test_it_returns_a_citable_finding(self) -> None:
        rows = [{"PLAN_TYPE": "basic", "MRR": 1.0}, {"PLAN_TYPE": "premium", "MRR": 9.0}]
        finding = run_governed_query(
            _intent(), validator=_Validator(), sql_generator=_SqlGenerator(rows),
        )
        assert finding.id == "F1" and finding.ok
        assert finding.rows == rows
        assert finding.sql == "SELECT 1", "sql must be carried for citation"
        assert finding.metric == "mrr"

    def test_ids_increment_across_a_diagnosis(self) -> None:
        existing = [Finding(id="F1", label="", metric="mrr", dimensions=[], rows=[])]
        finding = run_governed_query(
            _intent(), validator=_Validator(), sql_generator=_SqlGenerator(),
            existing=existing,
        )
        assert finding.id == "F2"

    def test_max_rows_caps_the_result(self) -> None:
        rows = [{"PLAN_TYPE": str(i), "MRR": float(i)} for i in range(50)]
        finding = run_governed_query(
            _intent(), validator=_Validator(), sql_generator=_SqlGenerator(rows),
            max_rows=10,
        )
        assert finding.row_count == 10

    def test_label_defaults_to_a_readable_description(self) -> None:
        finding = run_governed_query(
            _intent(), validator=_Validator(), sql_generator=_SqlGenerator())
        assert "mrr" in finding.label and "plan_type" in finding.label

    def test_no_dimension_reads_as_no_breakdown(self) -> None:
        finding = run_governed_query(
            build_probe_intent("mrr", []), validator=_Validator(),
            sql_generator=_SqlGenerator())
        assert "no breakdown" in finding.label


class TestFailureIsContained:
    def test_execution_error_returns_a_finding_rather_than_raising(self) -> None:
        """One dead probe costs its own slot, not the diagnosis."""
        finding = run_governed_query(
            _intent(), validator=_Validator(),
            sql_generator=_SqlGenerator(raises=RuntimeError("warehouse gone")),
        )
        assert not finding.ok
        assert "warehouse gone" in finding.error
        assert finding.rows == []
        assert finding.id == "F1", "a failed probe still consumes an id"

    def test_error_type_is_named_so_the_cause_is_diagnosable(self) -> None:
        finding = run_governed_query(
            _intent(), validator=_Validator(),
            sql_generator=_SqlGenerator(raises=TimeoutError("slow")),
        )
        assert finding.error.startswith("TimeoutError")


class TestCacheSharing:
    class _Cache:
        def __init__(self, payload=None):
            self.payload = payload
            self.gets: list = []

        def get(self, key):
            self.gets.append(key)
            return self.payload

    def test_a_hit_short_circuits_execution(self) -> None:
        cache = self._Cache({"result": {"data": [{"PLAN_TYPE": "basic", "MRR": 3.0}]}})
        sql_gen = _SqlGenerator()
        finding = run_governed_query(
            _intent(), validator=_Validator(), sql_generator=sql_gen, query_cache=cache,
        )
        assert finding.from_cache
        assert finding.rows == [{"PLAN_TYPE": "basic", "MRR": 3.0}]
        assert not sql_gen.executed, "cache hit should skip the warehouse"

    def test_a_miss_falls_through_and_executes(self) -> None:
        cache = self._Cache(None)
        sql_gen = _SqlGenerator()
        finding = run_governed_query(
            _intent(), validator=_Validator(), sql_generator=sql_gen, query_cache=cache,
        )
        assert not finding.from_cache and sql_gen.executed

    def test_cache_key_excludes_the_prose_query(self) -> None:
        """
        Keyed on intent, matching the NL route, so two phrasings of one question share
        an entry — and a probe can hit something a user query populated.
        """
        cache = self._Cache(None)
        run_governed_query(_intent(), validator=_Validator(),
                           sql_generator=_SqlGenerator(), query_cache=cache)
        key = cache.gets[0]
        assert "original_query" not in key
        assert "raw_llm_response" not in key
        assert key["metrics"] == ["mrr"]

    def test_validation_still_runs_before_the_cache_is_consulted(self) -> None:
        """A cached answer must not let an uncertified probe through."""
        cache = self._Cache({"result": {"data": [{"A": 1}]}})
        with pytest.raises(ProbeRejected):
            run_governed_query(
                _intent(), validator=_Validator(safe=False, violations=["x"]),
                sql_generator=_SqlGenerator(), query_cache=cache,
            )
        assert not cache.gets, "cache was consulted before governance"
