"""
tests/test_attribution_check.py — the third axis of dimension usability.

Compiling, being populated, and being ATTRIBUTED are three different things, and
the third was unchecked until now. A dimension can have fifteen healthy country
buckets and still leave a quarter of revenue in a NULL one: during the June-2026
orphan append, 16,602 payment rows lost their country to a LEFT JOIN that no
longer matched, putting 24.6% of 2026 revenue in an Unknown bucket. The
population check scored that pair `ok`, because the real buckets were still
there. A breakdown like that is not empty — it looks like an answer.

The two things worth pinning are the ARITHMETIC BOUNDARY (which measures may be
summed across buckets at all) and the SENTINEL ('unknown' counts as
unattributed, or the genre coalesce hides the very gap this looks for).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_AUDIT = Path(__file__).resolve().parents[1] / "audit_dimension_coverage.py"
_CSV = _AUDIT.parent / "dimension_coverage.csv"


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location("_audit_under_test", _AUDIT)
    module = importlib.util.module_from_spec(spec)
    # Registering in sys.modules is required: the module defines @dataclass, and
    # dataclasses resolves annotations via sys.modules[cls.__module__].
    sys.modules["_audit_under_test"] = module
    spec.loader.exec_module(module)
    return module


class TestAdditiveMetricBoundary:
    """
    Only `sum` and `count` may be summed across buckets. The exclusions are the
    point — each would produce a plausible number the arithmetic cannot support.
    """

    def test_real_manifest_classifies_the_boundary_cases(self, audit) -> None:
        from config import settings

        path = os.path.join(
            settings.dbt_project_dir, "target", "semantic_manifest.json"
        )
        if not os.path.exists(path):
            pytest.skip("semantic_manifest.json absent — run `dbt parse`")

        additive = audit._additive_metrics(path)

        # The motivating case, and ltv's numerator: the signal must be reachable.
        assert additive.get("total_revenue") == "sum"
        assert additive.get("mrr") == "sum"
        assert additive.get("total_sessions") == "count"

        # A mean of means is not the overall mean without weights.
        assert "avg_watch_time" not in additive
        assert "engagement_rate" not in additive
        # An entity in two buckets inflates a bucket-sum denominator, which
        # UNDERSTATES the unattributed share — the dangerous direction.
        assert "total_subscribers" not in additive
        # MetricFlow returns only the ratio; the volume is not in the result set.
        for ratio in ("churn_rate", "ltv", "recommendation_ctr",
                      "payment_failure_rate"):
            assert ratio not in additive, f"{ratio} is a ratio and cannot be summed"

    def test_a_missing_manifest_degrades_to_empty(self, audit) -> None:
        """Absent artifact means "cannot measure", never a crash."""
        assert audit._additive_metrics("does/not/exist.json") == {}


class TestAttributionArithmetic:
    @staticmethod
    def _rows(pairs):
        """(dimension_value, measure) pairs as MetricFlow would return them."""
        return ["country", "total_revenue"], [list(p) for p in pairs], 0

    def test_a_clean_dimension_is_attributed(self, audit) -> None:
        cols, rows, idx = self._rows([("US", 500.0), ("GB", 300.0), ("DE", 200.0)])
        verdict, pct = audit._check_attribution(
            cols, rows, idx, "total_revenue", "sum"
        )
        assert verdict == audit._ATTR_OK
        assert pct == pytest.approx(0.0)

    def test_the_june_2026_shape_is_flagged(self, audit) -> None:
        """
        The case this exists for: real buckets present, a quarter of the measure
        in a NULL one. `population` called that `ok`.
        """
        cols, rows, idx = self._rows([
            ("US", 500.0), ("GB", 300.0), ("DE", 230.0), (None, 336.0),
        ])
        verdict, pct = audit._check_attribution(
            cols, rows, idx, "total_revenue", "sum"
        )
        assert verdict == audit._ATTR_SKEWED
        assert pct == pytest.approx(24.6, abs=0.5)

    def test_unknown_counts_as_unattributed(self, audit) -> None:
        """
        NOT cosmetic. fct_stream_sessions coalesces a missing genre to 'unknown',
        so a NULL-only check reports 0.00% for the one dimension we know is 0.45%
        unattributed — the coalesce would hide exactly what this looks for.
        """
        cols, rows, idx = self._rows([("drama", 900.0), ("unknown", 100.0)])
        verdict, pct = audit._check_attribution(
            cols, rows, idx, "total_revenue", "sum"
        )
        assert pct == pytest.approx(10.0)
        assert verdict == audit._ATTR_SKEWED

    @pytest.mark.parametrize("sentinel", ["unknown", "UNKNOWN", " Unknown "])
    def test_the_sentinel_match_is_case_and_space_insensitive(
        self, audit, sentinel
    ) -> None:
        cols, rows, idx = self._rows([("drama", 900.0), (sentinel, 100.0)])
        _, pct = audit._check_attribution(cols, rows, idx, "total_revenue", "sum")
        assert pct == pytest.approx(10.0)

    def test_a_non_additive_measure_is_not_guessed_at(self, audit) -> None:
        """Reporting nothing beats a number the arithmetic cannot support."""
        cols, rows, idx = self._rows([("tv", 42.0), (None, 8.0)])
        verdict, pct = audit._check_attribution(
            cols, rows, idx, "total_revenue", None
        )
        assert verdict == audit._ATTR_NA
        assert pct is None

    def test_a_missing_measure_column_says_so(self, audit) -> None:
        """
        The measure is located BY NAME. If MetricFlow did not name it after the
        metric, summing whichever column looks numeric would judge the wrong one.
        """
        cols, rows, idx = self._rows([("US", 1.0)])
        verdict, pct = audit._check_attribution(
            cols, rows, idx, "not_a_column", "sum"
        )
        assert verdict == audit._ATTR_NA
        assert pct is None

    def test_zero_volume_is_not_a_percentage(self, audit) -> None:
        """A share of nothing is a division artefact; population reports it."""
        cols, rows, idx = self._rows([("US", 0.0), (None, 0.0)])
        verdict, pct = audit._check_attribution(
            cols, rows, idx, "total_revenue", "sum"
        )
        assert verdict == audit._ATTR_NA
        assert pct is None

    def test_null_measure_values_do_not_break_the_sum(self, audit) -> None:
        cols, rows, idx = self._rows([
            ("US", 900.0), ("GB", None), (None, 100.0),
        ])
        verdict, pct = audit._check_attribution(
            cols, rows, idx, "total_revenue", "sum"
        )
        assert verdict == audit._ATTR_SKEWED
        assert pct == pytest.approx(10.0)

    def test_the_threshold_is_a_boundary_not_a_range(self, audit) -> None:
        """Exactly at the threshold counts as skewed — a >= comparison."""
        at = audit._ATTR_THRESHOLD_PCT
        cols, rows, idx = self._rows([("US", 100.0 - at), (None, at)])
        verdict, _ = audit._check_attribution(
            cols, rows, idx, "total_revenue", "sum"
        )
        assert verdict == audit._ATTR_SKEWED


class TestCommittedArtifact:
    """The CSV is checked in, so its shape is part of the contract."""

    def test_the_csv_carries_the_attribution_columns(self) -> None:
        import csv

        if not _CSV.exists():
            pytest.skip("dimension_coverage.csv absent")
        with open(_CSV, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        assert "attribution" in header
        assert "unattributed_pct" in header

    def test_the_motivating_pair_is_clean_in_the_current_build(self) -> None:
        """
        `total_revenue x country` was 24.6% unattributed during the June-2026
        append, and repair_orphan_event_rows.py fixed it. A regression here means
        revenue-by-country answers are silently missing volume again.
        """
        import csv

        if not _CSV.exists():
            pytest.skip("dimension_coverage.csv absent")
        with open(_CSV, newline="", encoding="utf-8") as fh:
            rows = {(r["metric"], r["bare_dimension"]): r
                    for r in csv.DictReader(fh)}
        row = rows.get(("total_revenue", "country"))
        if row is None or not row.get("attribution"):
            pytest.skip("baseline written without execution")
        assert row["attribution"] == "attributed", (
            f"total_revenue x country is {row['unattributed_pct']}% "
            "unattributed — check for orphan payment rows"
        )
