"""
tests/test_eval_scoring.py — the eval scorer's notion of dimension identity.

`hallucination-002` ("Show me MRR by continent for last month") failed for two
independent reasons, and neither was the thing the case exists to test:

1. The scorer compared dimension names as raw strings, so `subscriber__country`
   did not match an expected `country`. The gateway treats those as the SAME
   dimension everywhere -- certification is checked on the bare name, and
   `format_mf_query()` passes anything containing `__` straight through -- so a
   correct answer scored as wrong. The prompt teaches the qualified form because
   `dims_section` runs names through `build_dimension_prefix_map()`.

2. The fixture did not offer `country` for `mrr` at all, so the mapping the case
   asks for (continent -> country, no clarification) was impossible. sem_mrr has
   always declared `subscriber` as a foreign entity; `dimension_coverage.csv`
   records `mrr,country,subscriber__country,true,ok,15,15`.

Both are test defects rather than model defects, which is exactly the kind of
failure that gets written off as "the LLM is flaky".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_GATEWAY = Path(__file__).resolve().parents[1]   # gateway/
_ROOT = _GATEWAY.parent                          # Streaming_Analytics/


def _load_harness():
    if str(_GATEWAY) not in sys.path:
        sys.path.insert(0, str(_GATEWAY))
    spec = importlib.util.spec_from_file_location(
        "_run_evals_under_test", _GATEWAY / "evals" / "run_evals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load_harness()


class TestBareDimension:
    @pytest.mark.parametrize("qualified,bare", [
        ("subscriber__country", "country"),
        ("subscription__plan_type", "plan_type"),
        ("session__device_type", "device_type"),
        ("subscription__period_month__month", "period_month"),
        ("country", "country"),
        ("plan_type", "plan_type"),
    ])
    def test_strips_the_entity_prefix(self, harness, qualified, bare) -> None:
        assert harness._bare_dim(qualified) == bare

    def test_it_agrees_with_the_gateway(self, harness) -> None:
        """
        The gateway's `SemanticValidator._get_bare_dimension()` is the source of
        truth for dimension identity. If the two ever diverge, the evals start
        scoring a vocabulary the gateway does not use.
        """
        from core.semantic_validator import SemanticValidator

        validator = SemanticValidator.__new__(SemanticValidator)
        for dim in ("subscriber__country", "country", "metric_time__month",
                    "subscription__period_month__month", "plan_type"):
            assert harness._bare_dim(dim) == validator._get_bare_dimension(dim), dim


class TestFixtureSupportsItsOwnGoldenCases:
    def test_mrr_offers_country(self, harness) -> None:
        """
        Without this, `hallucination-002` is unpassable: it expects the model to
        map "continent" onto `country` for `mrr`, and a model that declines a
        dimension its prompt never listed is behaving correctly.
        """
        assert "country" in harness._CERTIFIED_DIMENSIONS["mrr"]

    def test_every_expected_dimension_is_in_the_universe(self, harness) -> None:
        """
        Generalises the above: a golden case must never expect a dimension the
        fixture does not certify for at least one of that case's metrics.
        `--check-drift` cannot catch this -- it only reports the fixture claiming
        dimensions the REGISTRY lacks, never a golden case out-running the fixture.
        """
        import json

        path = _GATEWAY / "evals" / "golden_set.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases = raw if isinstance(raw, list) else raw.get("cases", raw.get("golden_set", []))

        offenders = []
        for case in cases:
            expected = case.get("expected", {})
            metrics = expected.get("metrics") or []
            for dim in expected.get("dimensions") or []:
                reachable = {
                    harness._bare_dim(d)
                    for m in metrics
                    for d in harness._CERTIFIED_DIMENSIONS.get(m, [])
                }
                if metrics and harness._bare_dim(dim) not in reachable:
                    offenders.append(f"{case.get('id')}: {dim} not certified for {metrics}")
        assert not offenders, "golden cases expect dimensions the fixture withholds: " + "; ".join(offenders)
