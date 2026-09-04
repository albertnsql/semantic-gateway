"""
tests/test_diagnostics_analysis.py — the contribution math.

`analysis.py` is what licenses the diagnostic path to state a cause. The narrative
prompt bans causal language because a model given three numbers will invent a reason;
the diagnostic answer is allowed to say "because" only because these functions
computed the attribution first. So this file carries more weight than its size
suggests, and it runs with no warehouse, no manifest, no LLM and no network.

The property that matters most is exactness: contributions must sum to the gap. If
they do not, every share is wrong and the answer is confidently misattributed. Both
decompositions are asserted against hand-worked numbers rather than against
themselves.
"""

from __future__ import annotations

import math

import pytest

from core.diagnostics.analysis import (
    DEFAULT_MIN_SHARE,
    build_hypothesis,
    clears_floor,
    concentration,
    decompose,
    explained_share,
    baseline_representativeness,
    is_broad_based,
    rank_hypotheses,
    rows_to_buckets,
)
from core.diagnostics.state import Bucket, Budget, Finding, next_finding_id


def b(label, value, weight=None):
    return Bucket(label=label, value=value, weight=weight)


class TestAdditiveDecomposition:
    """A sum metric: total_revenue, total_sessions, churned_subscribers."""

    def test_contributions_sum_to_the_gap(self) -> None:
        d = decompose(
            "country",
            target=[b("US", 500.0), b("DE", 100.0), b("GB", 200.0)],
            comparison=[b("US", 450.0), b("DE", 250.0), b("GB", 190.0)],
        )
        assert d.gap == pytest.approx(800.0 - 890.0)
        assert sum(c.delta for c in d.contributions) == pytest.approx(d.gap)
        assert d.exact

    def test_hand_worked_shares(self) -> None:
        """DE falls 150 of a 90 net shortfall; the others partly offset it."""
        d = decompose(
            "country",
            target=[b("US", 500.0), b("DE", 100.0), b("GB", 200.0)],
            comparison=[b("US", 450.0), b("DE", 250.0), b("GB", 190.0)],
        )
        by_label = {c.label: c for c in d.contributions}
        assert by_label["DE"].delta == pytest.approx(-150.0)
        assert by_label["US"].delta == pytest.approx(50.0)
        assert by_label["GB"].delta == pytest.approx(10.0)
        # DE moved 150 against a net gap of -90, so it more than explains it.
        assert by_label["DE"].share == pytest.approx(-150.0 / -90.0)

    def test_sorted_by_absolute_impact(self) -> None:
        d = decompose(
            "plan",
            target=[b("basic", 10.0), b("premium", 100.0)],
            comparison=[b("basic", 12.0), b("premium", 40.0)],
        )
        assert [c.label for c in d.contributions] == ["premium", "basic"]
        assert d.top is not None and d.top.label == "premium"

    def test_duplicate_labels_are_summed_not_dropped(self) -> None:
        """A grain mistake upstream should not silently discard half the data."""
        d = decompose("plan", target=[b("basic", 10.0), b("basic", 5.0)],
                      comparison=[b("basic", 12.0)])
        assert len(d.contributions) == 1
        assert d.contributions[0].target == pytest.approx(15.0)


class TestWeightedDecomposition:
    """
    A rate metric: churn_rate, engagement_rate, avg_watch_time.

    The whole reason this branch exists is to separate "we sold more cheap plans"
    from "each plan earns less". A single number cannot tell them apart.
    """

    def test_weighted_effects_are_exact(self) -> None:
        d = decompose(
            "plan",
            target=[b("basic", 5.0, weight=800.0), b("premium", 20.0, weight=200.0)],
            comparison=[b("basic", 5.0, weight=500.0), b("premium", 20.0, weight=500.0)],
        )
        assert d.weighted
        assert sum(c.delta for c in d.contributions) == pytest.approx(d.gap)
        assert d.exact

    def test_pure_mix_shift_has_no_rate_effect(self) -> None:
        """Rates identical on both sides, only the mix moved."""
        d = decompose(
            "plan",
            target=[b("basic", 5.0, weight=800.0), b("premium", 20.0, weight=200.0)],
            comparison=[b("basic", 5.0, weight=500.0), b("premium", 20.0, weight=500.0)],
        )
        for c in d.contributions:
            assert c.rate_effect == pytest.approx(0.0)
            assert c.mix_effect is not None and abs(c.mix_effect) > 0
        # basic +300 at rate 5 = +1500; premium -300 at rate 20 = -6000
        by_label = {c.label: c for c in d.contributions}
        assert by_label["basic"].mix_effect == pytest.approx(1500.0)
        assert by_label["premium"].mix_effect == pytest.approx(-6000.0)
        assert d.gap == pytest.approx(-4500.0)

    def test_pure_rate_change_has_no_mix_effect(self) -> None:
        """Weights identical, only within-bucket performance moved."""
        d = decompose(
            "plan",
            target=[b("basic", 4.0, weight=500.0), b("premium", 18.0, weight=500.0)],
            comparison=[b("basic", 5.0, weight=500.0), b("premium", 20.0, weight=500.0)],
        )
        for c in d.contributions:
            assert c.mix_effect == pytest.approx(0.0)
        by_label = {c.label: c for c in d.contributions}
        assert by_label["basic"].rate_effect == pytest.approx(-500.0)
        assert by_label["premium"].rate_effect == pytest.approx(-1000.0)

    def test_mix_and_rate_together_still_sum_exactly(self) -> None:
        """Both moved — the case where a naive read misattributes."""
        d = decompose(
            "plan",
            target=[b("basic", 4.0, weight=900.0), b("premium", 25.0, weight=100.0)],
            comparison=[b("basic", 5.0, weight=500.0), b("premium", 20.0, weight=500.0)],
        )
        assert sum((c.mix_effect or 0) + (c.rate_effect or 0)
                   for c in d.contributions) == pytest.approx(d.gap)
        assert d.exact

    def test_partial_weights_degrade_to_additive_and_say_so(self) -> None:
        """
        A half-weighted input must not be treated as weighted: the resulting total
        would match neither decomposition.
        """
        d = decompose("plan", target=[b("basic", 5.0, weight=100.0), b("premium", 20.0)],
                      comparison=[b("basic", 5.0, weight=100.0)])
        assert not d.weighted
        assert "additive" in d.note
        assert d.exact


class TestEdgeCasesThatWouldOtherwiseLie:
    def test_zero_gap_yields_no_shares_not_infinities(self) -> None:
        d = decompose("plan", target=[b("basic", 10.0)], comparison=[b("basic", 10.0)])
        assert d.gap == 0.0
        assert all(c.share == 0.0 for c in d.contributions)
        assert all(math.isfinite(c.share) for c in d.contributions)

    def test_bucket_present_on_one_side_only_is_kept_and_flagged(self) -> None:
        """A launched or retired segment is a finding, not something to drop."""
        d = decompose(
            "plan",
            target=[b("basic", 10.0), b("new_tier", 40.0)],
            comparison=[b("basic", 12.0), b("retired_tier", 5.0)],
        )
        by_label = {c.label: c for c in d.contributions}
        assert by_label["new_tier"].presence == "target_only"
        assert by_label["retired_tier"].presence == "comparison_only"
        assert by_label["retired_tier"].delta == pytest.approx(-5.0)
        assert d.exact, "dropping one-sided buckets would break exactness"

    def test_the_null_bucket_survives(self) -> None:
        """
        Unattributed volume must stay visible. 24.6% of 2026 revenue once sat in an
        Unknown country bucket; a decomposition that hides it repeats that.
        """
        d = decompose("country", target=[b("US", 10.0), b(None, 90.0)],
                      comparison=[b("US", 10.0), b(None, 10.0)])
        null_c = next(c for c in d.contributions if c.is_null_bucket)
        assert null_c.delta == pytest.approx(80.0)
        assert d.exact

    def test_empty_inputs_do_not_raise(self) -> None:
        d = decompose("plan", target=[], comparison=[])
        assert d.contributions == [] and d.gap == 0.0 and d.exact

    def test_target_only_side(self) -> None:
        d = decompose("plan", target=[b("basic", 10.0)], comparison=[])
        assert d.gap == pytest.approx(10.0)
        assert d.contributions[0].presence == "target_only"
        assert d.exact


class TestConcentrationAndFloors:
    def test_concentrated_gap(self) -> None:
        d = decompose("country",
                      target=[b("DE", 10.0), b("US", 100.0), b("GB", 100.0)],
                      comparison=[b("DE", 110.0), b("US", 100.0), b("GB", 100.0)])
        top_share, to_eighty = concentration(d)
        assert top_share == pytest.approx(1.0)
        assert to_eighty == 1

    def test_spread_gap_needs_several_buckets(self) -> None:
        d = decompose("country",
                      target=[b(x, 0.0) for x in "abcde"],
                      comparison=[b(x, 20.0) for x in "abcde"])
        top_share, to_eighty = concentration(d)
        assert top_share == pytest.approx(0.2)
        assert to_eighty == 4  # 0.2 * 4 = 0.8

    def test_offsetting_movements_do_not_break_the_count(self) -> None:
        """Only same-signed buckets count, or cumulative share overshoots 1."""
        d = decompose("plan", target=[b("a", 0.0), b("up", 50.0)],
                      comparison=[b("a", 100.0), b("up", 0.0)])
        top_share, to_eighty = concentration(d)
        assert 0.0 < top_share <= 2.0
        assert to_eighty >= 1

    def test_floor_rejects_a_trivial_absolute_gap(self) -> None:
        d = decompose("plan", target=[b("a", 1.0000000001)], comparison=[b("a", 1.0)])
        assert not clears_floor(d, min_absolute=0.01)

    def test_floor_rejects_a_diffuse_gap(self) -> None:
        d = decompose("country",
                      target=[b(str(i), 0.0) for i in range(40)],
                      comparison=[b(str(i), 10.0) for i in range(40)])
        assert not clears_floor(d, min_share=DEFAULT_MIN_SHARE)

    def test_floor_accepts_a_real_concentrated_gap(self) -> None:
        d = decompose("country", target=[b("DE", 100.0), b("US", 900.0)],
                      comparison=[b("DE", 400.0), b("US", 900.0)])
        assert clears_floor(d)

    def test_explained_share_measures_aligned_pull(self) -> None:
        d = decompose("plan", target=[b("down", 0.0), b("up", 20.0)],
                      comparison=[b("down", 100.0), b("up", 0.0)])
        # gap = -80; aligned (negative) pull is -100 => 1.25
        assert explained_share(d) == pytest.approx(1.25)


class TestHypothesisWording:
    """The statement is generated here, not by a model. It must not overclaim."""

    def test_strong_single_cause_says_explains(self) -> None:
        d = decompose("country", target=[b("DE", 10.0), b("US", 100.0)],
                      comparison=[b("DE", 110.0), b("US", 100.0)])
        h = build_hypothesis(d, evidence=["F1", "F2"])
        assert h.verdict == "explains"
        assert "DE" in h.statement and "%" in h.statement
        assert h.evidence == ["F1", "F2"]
        assert h.confidence == "contribution"

    def test_diffuse_gap_is_reported_as_not_it(self) -> None:
        d = decompose("country",
                      target=[b(str(i), 0.0) for i in range(40)],
                      comparison=[b(str(i), 10.0) for i in range(40)])
        h = build_hypothesis(d)
        assert h.verdict == "not_it"
        assert "does not explain" in h.statement
        assert h.reportable, "a ruled-out hypothesis is still worth stating"

    def test_no_material_gap_is_inconclusive_not_not_it(self) -> None:
        """Distinct outcomes: 'I checked and it is not the cause' vs 'I cannot tell'."""
        d = decompose("plan", target=[b("a", 5.0)], comparison=[b("a", 5.0)])
        h = build_hypothesis(d)
        assert h.verdict == "inconclusive"
        assert not h.reportable

    def test_weighted_statement_names_mix_or_rate(self) -> None:
        d = decompose(
            "plan",
            target=[b("basic", 5.0, weight=900.0), b("premium", 20.0, weight=100.0)],
            comparison=[b("basic", 5.0, weight=500.0), b("premium", 20.0, weight=500.0)],
        )
        h = build_hypothesis(d)
        assert "composition" in h.statement or "within-bucket rate" in h.statement
        assert "mix" in h.statement and "rate" in h.statement

    def test_one_sided_bucket_is_disclosed(self) -> None:
        d = decompose("plan", target=[b("new_tier", 500.0), b("basic", 10.0)],
                      comparison=[b("basic", 10.0)])
        h = build_hypothesis(d)
        assert "only in the target period" in h.statement

    def test_null_bucket_is_flagged_as_possible_data_gap(self) -> None:
        """The genre incident in prose form: do not report a data gap as a segment."""
        d = decompose("genre", target=[b(None, 200.0), b("drama", 10.0)],
                      comparison=[b(None, 10.0), b("drama", 10.0)])
        h = build_hypothesis(d)
        assert "unattributed" in h.statement
        assert "data gap" in h.statement

    def test_non_exact_decomposition_refuses_to_conclude(self) -> None:
        from core.diagnostics.state import Contribution, Decomposition

        broken = Decomposition(
            dimension="plan", target_total=100.0, comparison_total=0.0, gap=100.0,
            contributions=[Contribution("a", 10.0, 0.0, 10.0, 0.1)], residual=90.0,
        )
        assert not broken.exact
        h = build_hypothesis(broken)
        assert h.verdict == "inconclusive"
        assert "residual" in h.statement


class TestBaselineRepresentativeness:
    """
    A gap is only as meaningful as what it is measured against.

    The case: a live churn diagnosis reported +163.7% for June 2026 against May.
    Both correct — but May was ~40% below the trailing 12-month average, so the
    comparison maximised the apparent jump. Against trend June is ~+60%.
    """

    #: The real monthly churn series, Jun 2025 - May 2026, from the warehouse.
    TREND = [0.0324, 0.0322, 0.0296, 0.0273, 0.0252, 0.0222,
             0.0276, 0.0276, 0.0153, 0.0181, 0.0154, 0.0176]
    MAY = 0.0154
    JUNE = 0.0406

    def test_it_flags_the_may_baseline(self) -> None:
        check = baseline_representativeness(self.MAY, self.TREND, target_value=self.JUNE)
        assert check is not None
        assert not check.representative
        assert check.deviation < -0.30, f"May read as only {check.deviation:.0%} off"
        assert check.direction == "below"
        assert check.trend_relative_gap == pytest.approx(0.6, abs=0.15), (
            "the trend-relative figure is the honest headline"
        )
        assert check.trend_months == 12

    def test_a_typical_baseline_passes_quietly(self) -> None:
        typical = sum(self.TREND) / len(self.TREND)
        check = baseline_representativeness(typical, self.TREND)
        assert check is not None and check.representative
        assert abs(check.deviation) < 0.01

    def test_it_averages_monthly_values_rather_than_dividing_an_aggregate(self) -> None:
        """
        The first version took the metric aggregated over 12 months and divided by
        12. For a ratio that is not a rate: churn_rate over 2025-06..2026-05 returns
        0.206, because the denominator is a DISTINCT subscriber count over the whole
        window. 0.206/12 = 1.72% against a true monthly mean of 2.61%, and the check
        then called a 40%-below-average baseline representative.
        """
        aggregate_over_window = 0.206   # the real value MetricFlow returned
        naive = aggregate_over_window / 12
        correct = sum(self.TREND) / len(self.TREND)
        # Relative, not absolute: these are rates in the low percents, so an absolute
        # gap looks tiny while the error is large. 1.72% against 2.42% is ~29% off.
        assert abs(naive - correct) / correct > 0.20, (
            f"fixture no longer demonstrates the bug: naive={naive:.4f} "
            f"correct={correct:.4f}"
        )

        check = baseline_representativeness(self.MAY, self.TREND)
        assert check.trend_per_month == pytest.approx(correct, abs=1e-6)
        assert not check.representative

    def test_a_single_trend_month_yields_no_verdict(self) -> None:
        """One month is not a trend; a verdict from it would be noise."""
        assert baseline_representativeness(self.MAY, [0.02]) is None

    def test_an_empty_or_zero_trend_yields_no_verdict(self) -> None:
        """
        None, not representative=True. "Could not tell" and "was typical" read
        identically in an answer and only one is honest.
        """
        assert baseline_representativeness(self.MAY, []) is None
        assert baseline_representativeness(self.MAY, [0.0, 0.0]) is None

    def test_nones_in_the_series_are_dropped(self) -> None:
        check = baseline_representativeness(self.MAY, [0.03, None, 0.03])
        assert check is not None and check.trend_months == 2

    def test_it_flags_an_unusually_HIGH_baseline_too(self) -> None:
        """Comparing against a peak understates a real rise just as badly."""
        check = baseline_representativeness(0.05, self.TREND)
        assert not check.representative and check.direction == "above"

    def test_target_is_optional(self) -> None:
        check = baseline_representativeness(self.MAY, self.TREND)
        assert check is not None and check.trend_relative_gap is None

class TestBroadBasedVerdict:
    """
    "Spread across every axis" is itself a finding, and saying it beats listing the
    largest bucket on each axis as though any of them were the cause.

    A live total_revenue run produced three lines reading "39.3% ... so it is spread",
    "43.6% ... so it is spread", "27.3% ... so it is spread" — honest, and useless.
    """

    @staticmethod
    def _spread(dimension: str, buckets: int):
        """Every bucket moves equally, so no single value dominates."""
        return build_hypothesis(
            decompose(
                dimension,
                target=[b(f"{dimension}{i}", 0.0) for i in range(buckets)],
                comparison=[b(f"{dimension}{i}", 10.0) for i in range(buckets)],
            )
        )

    @staticmethod
    def _concentrated(dimension: str):
        return build_hypothesis(
            decompose(dimension, target=[b("a", 0.0), b("b", 100.0)],
                      comparison=[b("a", 100.0), b("b", 100.0)])
        )

    def test_several_spread_axes_read_as_broad_based(self) -> None:
        assert is_broad_based([self._spread("d1", 5), self._spread("d2", 6),
                               self._spread("d3", 4)])

    def test_one_concentrated_axis_disqualifies_it(self) -> None:
        """That axis IS the answer, so it must not be diluted into 'broad-based'."""
        assert not is_broad_based([self._spread("d1", 5), self._concentrated("d2")])

    def test_a_single_axis_is_never_broad_based(self) -> None:
        """One dimension is not evidence about breadth — it is one slice."""
        assert not is_broad_based([self._spread("d1", 5)])

    def test_no_hypotheses_is_not_broad_based(self) -> None:
        assert not is_broad_based([])

    def test_the_live_revenue_case_reads_as_broad_based(self) -> None:
        """
        The exact numbers from the live run: a gap of 165,534 with top movers of
        72,243 (43.6%), 65,079 (39.3%) and 45,209 (27.3%) across axes of 3, 9 and 6
        buckets.

        The first attempt gated on buckets-to-80% and failed here, because two of
        three plan types is proportionally the same as ten of fifteen countries.
        """
        def axis(name, top_delta, other_deltas):
            base = 100_000.0
            deltas = [top_delta] + list(other_deltas)
            return build_hypothesis(decompose(
                name,
                target=[b(f"{name}{i}", base + d) for i, d in enumerate(deltas)],
                comparison=[b(f"{name}{i}", base) for i in range(len(deltas))],
            ))

        plans = axis("plan", 72_243.0, [50_000.0, 43_291.0])          # 43.6%
        countries = axis("country", 65_079.0, [12_557.0] * 8)          # 39.3%
        channels = axis("channel", 45_209.0, [24_065.0] * 5)          # 27.3%

        for h, expected in ((plans, 0.436), (countries, 0.393), (channels, 0.273)):
            top_share, _ = concentration(h.decomposition)
            assert top_share == pytest.approx(expected, abs=0.01), (
                f"{h.dimension} fixture drifted from the live case: {top_share:.1%}"
            )
        assert is_broad_based([plans, countries, channels])

    def test_a_dominant_axis_leads_instead(self) -> None:
        """
        The live churn case: plan_type at 57.6% is the story, so the answer must name
        it rather than dissolving it into "broad-based".
        """
        dominant = build_hypothesis(decompose(
            "plan_type", target=[b("standard", 42.4), b("premium", 30.0), b("basic", 27.6)],
            comparison=[b("standard", 100.0), b("premium", 60.0), b("basic", 55.0)]))
        top_share, _ = concentration(dominant.decomposition)
        assert top_share >= 0.50, f"fixture is not dominant enough: {top_share:.1%}"
        assert not is_broad_based([dominant, self._spread("d2", 6)])

    def test_hypotheses_without_a_decomposition_are_ignored(self) -> None:
        from core.diagnostics.state import Hypothesis as H

        bare = H(dimension="x", statement="", confidence="contribution",
                 verdict="inconclusive", explained_share=0.0)
        assert not is_broad_based([bare, bare, bare])


class TestRanking:
    def test_contribution_outranks_association_regardless_of_size(self) -> None:
        """
        A big correlation is still not an explanation. Letting it sort above a
        smaller arithmetic attribution would invert the confidence tiers.
        """
        weak_contribution = build_hypothesis(
            decompose("plan", target=[b("a", 90.0), b("b", 10.0)],
                      comparison=[b("a", 100.0), b("b", 10.0)]),
        )
        strong_association = build_hypothesis(
            decompose("buffering", target=[b("x", 0.0)], comparison=[b("x", 100.0)]),
            confidence="association",
        )
        ranked = rank_hypotheses([strong_association, weak_contribution])
        assert ranked[0].confidence == "contribution"

    def test_a_complete_tie_resolves_deterministically(self) -> None:
        """
        ltv's three axes tie exactly on (contribution, partial, 100%). Without a final
        tie-break the order fell through to input order, which depends on raw floats
        that are not bit-reproducible - DuckDB aggregates in parallel and float
        addition is not associative. Three consecutive runs of the same live question
        ranked them three different ways, which a user re-asking would see.
        """
        tied = [
            build_hypothesis(decompose(
                dim, target=[b("a", 0.0), b("b", 50.0)],
                comparison=[b("a", 50.0), b("b", 50.0)]))
            for dim in ("plan_type", "country", "acquisition_channel")
        ]
        assert len({h.explained_share for h in tied}) == 1, "fixture is not tied"

        import random

        for _ in range(5):
            shuffled = tied[:]
            random.shuffle(shuffled)
            assert [h.dimension for h in rank_hypotheses(shuffled)] == [
                "acquisition_channel", "country", "plan_type"
            ]

    def test_a_near_tie_is_treated_as_tied(self) -> None:
        """
        Shares equal to any meaningful precision must not be separated by float noise
        in the 12th decimal place, or the ordering is unstable again.
        """
        from core.diagnostics.state import Hypothesis as H

        a = H(dimension="zzz", statement="", confidence="contribution",
              verdict="partial", explained_share=1.0)
        bb = H(dimension="aaa", statement="", confidence="contribution",
               verdict="partial", explained_share=1.0 + 1e-12)
        assert [h.dimension for h in rank_hypotheses([a, bb])] == ["aaa", "zzz"]

    def test_ruled_out_sorts_last_but_is_not_dropped(self) -> None:
        explains = build_hypothesis(
            decompose("c", target=[b("DE", 0.0), b("US", 100.0)],
                      comparison=[b("DE", 100.0), b("US", 100.0)]))
        not_it = build_hypothesis(
            decompose("d", target=[b(str(i), 0.0) for i in range(40)],
                      comparison=[b(str(i), 10.0) for i in range(40)]))
        ranked = rank_hypotheses([not_it, explains])
        assert [h.verdict for h in ranked] == ["explains", "not_it"]
        assert len(ranked) == 2


class TestRowsToBuckets:
    def test_uppercase_columns_are_matched(self) -> None:
        """
        DuckDBPool.execute() uppercases every key to preserve Snowflake DictCursor
        behaviour. A caller passing the lowercase semantic name must still match.
        """
        rows = [{"SUBSCRIBER__COUNTRY": "US", "TOTAL_REVENUE": 10.0},
                {"SUBSCRIBER__COUNTRY": "DE", "TOTAL_REVENUE": 4.0}]
        buckets = rows_to_buckets(rows, "subscriber__country", "total_revenue")
        assert {x.label for x in buckets} == {"US", "DE"}
        assert sum(x.value for x in buckets) == pytest.approx(14.0)

    def test_weight_column_is_optional(self) -> None:
        rows = [{"PLAN": "basic", "RATE": 5.0, "SUBS": 100.0}]
        assert rows_to_buckets(rows, "plan", "rate")[0].weight is None
        assert rows_to_buckets(rows, "plan", "rate", "subs")[0].weight == 100.0

    def test_null_dimension_is_kept_null_value_is_skipped(self) -> None:
        rows = [{"G": None, "V": 5.0}, {"G": "drama", "V": None}]
        buckets = rows_to_buckets(rows, "g", "v")
        assert len(buckets) == 1 and buckets[0].label is None

    def test_missing_column_raises_rather_than_returning_empty(self) -> None:
        """An empty decomposition would read as 'no gap' instead of 'wrong column'."""
        with pytest.raises(KeyError, match="nope"):
            rows_to_buckets([{"G": "x", "V": 1.0}], "nope", "v")

    def test_empty_rows(self) -> None:
        assert rows_to_buckets([], "g", "v") == []

    def test_decimal_values_from_the_warehouse_are_coerced(self) -> None:
        """
        DuckDB returns DECIMAL columns as `Decimal`, so revenue arrives as
        Decimal('439707.93'). Observed in a live probe. Left uncoerced it survives
        arithmetic but breaks on mixing with float, and Decimal/float division raises
        — which would surface as a crash mid-decomposition rather than as bad numbers.
        """
        from decimal import Decimal

        rows = [{"PLAN": "premium", "TOTAL_REVENUE": Decimal("463300.71")},
                {"PLAN": "basic", "TOTAL_REVENUE": Decimal("127386.24")}]
        buckets = rows_to_buckets(rows, "plan", "total_revenue")
        assert all(isinstance(x.value, float) for x in buckets)
        assert sum(x.value for x in buckets) == pytest.approx(590686.95)

    def test_integer_weights_are_coerced_too(self) -> None:
        rows = [{"PLAN": "basic", "RATE": 0.04, "SUBS": 1200}]
        assert isinstance(rows_to_buckets(rows, "plan", "rate", "subs")[0].weight, float)


class TestBudget:
    def test_not_spent_when_fresh(self) -> None:
        assert not Budget().spent

    @pytest.mark.parametrize(
        "kwargs, needle",
        [
            ({"rounds_used": 3}, "round"),
            ({"probes_used": 12}, "probe"),
            ({"seconds_used": 25.0}, "deadline"),
        ],
    )
    def test_each_limit_stops_the_loop_and_explains_itself(self, kwargs, needle) -> None:
        budget = Budget(**kwargs)
        assert budget.spent
        assert needle in budget.why_spent()

    def test_probes_remaining_never_negative(self) -> None:
        assert Budget(probes_used=99).probes_remaining == 0


class TestPurity:
    """
    `analysis.py` and `state.py` must stay free of the gateway's heavy dependencies.

    That is not tidiness -- it is what keeps this file runnable in milliseconds with
    no warehouse, no manifest and no credentials, and this is the module whose
    correctness the causal claims rest on. The easiest way to lose it is an
    innocent-looking `from config import settings` added for one default value.
    """

    def test_no_heavy_imports_are_pulled_in(self) -> None:
        import subprocess
        import sys

        code = (
            "import sys, core.diagnostics.analysis;"
            "print(','.join(m for m in "
            "('config','duckdb','openai','metricflow','dbt','chromadb','httpx') "
            "if m in sys.modules))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, proc.stderr[-500:]
        leaked = proc.stdout.strip()
        assert not leaked, f"analysis.py now pulls in: {leaked}"


class TestFindingIds:
    def test_ids_are_sequential_and_citable(self) -> None:
        findings: list[Finding] = []
        assert next_finding_id(findings) == "F1"
        findings.append(Finding(id="F1", label="x", metric="mrr", dimensions=[], rows=[]))
        assert next_finding_id(findings) == "F2"

    def test_error_marks_a_finding_not_ok(self) -> None:
        bad = Finding(id="F1", label="x", metric="mrr", dimensions=[], rows=[],
                      error="boom")
        assert not bad.ok and bad.row_count == 0
