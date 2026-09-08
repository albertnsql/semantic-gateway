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
    bucket_lift,
    DEFAULT_MIN_LIFT,
    leading_driver,
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
from core.diagnostics.state import (
    Bucket,
    Budget,
    Contribution,
    Finding,
    next_finding_id,
)


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

        The BASE distribution is load-bearing and was not, originally. The first
        version of this fixture gave every bucket an identical base of 100,000,
        which reproduced the top shares and invented the weights. That was harmless
        while the verdict was share-only, and wrong once lift entered: a uniform
        base turns `country0` into 11.1% of the base carrying 39.3% of the gap — a
        3.54x lift, i.e. strongly concentrated — and the fixture then contradicted
        the very verdict it exists to pin.

        The weights below are MEASURED from the warehouse for total_revenue,
        H1-2026 vs H2-2025, so the case is grounded in both dimensions::

            plan_type    premium 42.1% base / 50.7% gap = 1.20x
            country      US      37.8% base / 40.3% gap = 1.06x
            payment      card    24.8% base / 26.6% gap = 1.08x

        Growth really was proportional to size on every axis — which is what
        "broad-based" means, and what a share-only test got right by luck here.
        """
        def axis(name, base_shares, gap_shares):
            """Buckets with the real base weights and the real gap shares."""
            total_base = 300_000.0
            gap = 165_534.0
            target, comparison = [], []
            for i, (bs, gs) in enumerate(zip(base_shares, gap_shares)):
                base = total_base * bs
                comparison.append(b(f"{name}{i}", base))
                target.append(b(f"{name}{i}", base + gap * gs))
            return build_hypothesis(decompose(name, target=target,
                                              comparison=comparison))

        plans = axis("plan", [0.421, 0.445, 0.134], [0.507, 0.371, 0.122])
        countries = axis(
            "country",
            [0.378, 0.120, 0.082, 0.057] + [0.0726] * 5,
            [0.403, 0.137, 0.056, 0.075] + [0.0658] * 5,
        )
        channels = axis("channel", [0.248, 0.249, 0.250, 0.253],
                        [0.266, 0.250, 0.243, 0.240])

        for h in (plans, countries, channels):
            top_share, _ = concentration(h.decomposition)
            assert top_share < 0.60, (
                f"{h.dimension} fixture is no longer a spread case: {top_share:.1%}"
            )
        assert is_broad_based([plans, countries, channels]), (
            "measured revenue growth is proportional to size on every axis"
        )

    def test_a_dominant_axis_leads_instead(self) -> None:
        """
        The live churn case — rebuilt, because the original fixture encoded the bug.

        It asserted that `standard` at 57.6% of the gap was "the story". In that
        fixture standard was 46.5% of the base, so 57.6% is a lift of 1.08 —
        proportional, and every other bucket sat at ~0.94. Nothing over-contributed,
        so "broad-based" was the correct reading and the test was pinning the very
        failure mode lift exists to remove: naming the largest bucket.

        The MEASURED June-2026 numbers tell a different and sharper story::

            bucket      base    gap    lift
            standard   45.5%  47.2%   1.04x   <- merely the biggest
            basic      22.2%  37.6%   1.69x   <- the driver
            premium    32.3%  15.2%   0.47x

        which is exactly what CLAUDE.md records: "standard is the largest plan, so
        it wins on count while basic is worst on rate."
        """
        base_total = 1000.0
        gap = -324.0
        weights = {"standard": 0.455, "basic": 0.222, "premium": 0.323}
        gap_shares = {"standard": 0.472, "basic": 0.376, "premium": 0.152}
        comparison = [b(k, base_total * w) for k, w in weights.items()]
        target = [b(k, base_total * weights[k] + gap * gap_shares[k])
                  for k in weights]
        dominant = build_hypothesis(decompose("plan_type", target=target,
                                              comparison=comparison))

        driver = leading_driver(dominant.decomposition)
        assert driver is not None, "fixture has no over-contributing bucket"
        assert driver.label == "basic", (
            f"named {driver.label!r} — the largest bucket is `standard`, but it "
            "carries the gap in proportion to its size"
        )
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

    # Derived from the defaults rather than hardcoded. These were literals
    # (rounds 3 / probes 12 / seconds 25.0) and the probe one silently stopped
    # testing anything when `max_probes` went 12 -> 40 for the reflect loop: 12 no
    # longer exhausts the budget, so `spent` was False and the assertion failed for
    # the right reason but the wrong cause.
    @pytest.mark.parametrize("field, needle", [
        ("rounds_used", "round"),
        ("probes_used", "probe"),
        ("seconds_used", "deadline"),
    ])
    def test_each_limit_stops_the_loop_and_explains_itself(self, field, needle) -> None:
        limit = {
            "rounds_used": Budget().max_rounds,
            "probes_used": Budget().max_probes,
            "seconds_used": Budget().deadline_seconds,
        }[field]
        budget = Budget(**{field: limit})
        assert budget.spent, f"{field}={limit} should exhaust the budget"
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


class TestLift:
    """
    A bucket drives a movement when it carries MORE of it than its size implies —
    not when it is merely large.

    Without this the largest bucket wins by construction, because
    `Decomposition.top` is `contributions[0]` sorted by absolute delta. A live
    churn diagnosis named `plan_type = standard` (45.5% of the base, 47.2% of the
    gap, lift 1.04) and `billing_cycle = monthly` (72.2% base, 76.1% gap, lift
    1.05) as causes. Both are arithmetic restatements of "the metric moved".

    Low-cardinality axes are where it bites hardest: with two buckets one MUST
    hold >= 50% of the gap unless they are near-balanced, so a share-only test
    grades a binary dimension as explaining almost any movement. That is exactly
    how four of nine snapshot scenarios flipped to "found the cause".
    """

    @staticmethod
    def _axis(name, weights, gap_shares, *, base_total=1000.0, gap=-324.0):
        """Buckets with explicit base weights and gap shares."""
        comparison = [b(k, base_total * w) for k, w in weights.items()]
        target = [b(k, base_total * weights[k] + gap * gap_shares[k])
                  for k in weights]
        return decompose(name, target=target, comparison=comparison)

    def test_a_proportional_bucket_is_not_a_driver(self) -> None:
        """The June churn case, measured. `standard` is biggest and proportional."""
        d = self._axis(
            "plan_type",
            {"standard": 0.455, "basic": 0.222, "premium": 0.323},
            {"standard": 0.472, "basic": 0.376, "premium": 0.152},
        )
        top = d.top
        assert top is not None and top.label == "standard", (
            "fixture assumption: standard carries the largest absolute delta"
        )
        assert bucket_lift(top, d.comparison_total) == pytest.approx(1.04, abs=0.03)

        driver = leading_driver(d)
        assert driver is not None and driver.label == "basic", (
            "the driver is the over-contributing bucket, not the biggest one"
        )
        assert bucket_lift(driver, d.comparison_total) == pytest.approx(1.69, abs=0.05)

    def test_a_binary_axis_needs_more_than_a_big_share(self) -> None:
        """
        `billing_cycle` has two buckets, so one always holds most of the gap.
        Monthly carried 76.1% of the June churn gap off 72.2% of the base.
        """
        d = self._axis("billing_cycle", {"monthly": 0.722, "annual": 0.278},
                       {"monthly": 0.761, "annual": 0.239})
        top = d.top
        assert abs(top.delta / d.gap) > 0.60, "fixture: monthly dominates on share"
        assert bucket_lift(top, d.comparison_total) == pytest.approx(1.05, abs=0.03)
        assert leading_driver(d) is None, (
            "a 76% share at 1.05x lift is proportional, not a cause"
        )

    def test_a_small_bucket_with_high_lift_is_not_the_driver(self) -> None:
        """
        Measured revenue growth: `country = DE` is 5.7% of the base and carries
        7.5% of a +181,599 gap — a lift of 1.31, and a fourteenth of the movement.
        Lift alone, at the 5% reporting floor, would report DE as the cause.
        """
        d = self._axis(
            "country",
            {"US": 0.378, "IN": 0.120, "GB": 0.082, "DE": 0.057, "other": 0.363},
            {"US": 0.403, "IN": 0.137, "GB": 0.056, "DE": 0.075, "other": 0.329},
            gap=181_599.0,
        )
        de = next(c for c in d.contributions if c.label == "DE")
        assert bucket_lift(de, d.comparison_total) >= DEFAULT_MIN_LIFT, (
            "fixture: DE does over-contribute"
        )
        assert leading_driver(d) is None or leading_driver(d).label != "DE", (
            "DE clears the lift floor but carries too little of the gap"
        )

    def test_measured_revenue_growth_has_no_driver_on_any_axis(self) -> None:
        """Growth proportional to size on every axis is the definition of broad."""
        plans = self._axis("plan_type",
                           {"premium": 0.421, "standard": 0.445, "basic": 0.134},
                           {"premium": 0.507, "standard": 0.371, "basic": 0.122},
                           gap=181_599.0)
        methods = self._axis(
            "payment_method",
            {"card": 0.248, "paypal": 0.249, "gp": 0.250, "app": 0.253},
            {"card": 0.266, "paypal": 0.250, "gp": 0.243, "app": 0.240},
            gap=181_599.0,
        )
        for d in (plans, methods):
            assert leading_driver(d) is None, f"{d.dimension} should have no driver"

    def test_a_segment_with_no_baseline_counts_as_over_contributing(self) -> None:
        """
        A bucket absent from the comparison window has no lift to compute — and is
        a categorically different finding, a segment that did not exist before.
        Treated as over-contributing rather than given an invented ratio.
        """
        d = decompose("plan_type",
                      target=[b("new_tier", 400.0), b("standard", 600.0)],
                      comparison=[b("standard", 600.0)])
        new = next(c for c in d.contributions if c.label == "new_tier")
        assert bucket_lift(new, d.comparison_total) is None
        driver = leading_driver(d)
        assert driver is not None and driver.label == "new_tier"

    def test_lift_is_none_when_there_is_no_comparison_volume(self) -> None:
        """Guards a division by zero on an empty comparison window."""
        contribution = Contribution(label="a", target=10.0, comparison=0.0,
                                    delta=10.0, share=1.0)
        assert bucket_lift(contribution, 0.0) is None

    def test_no_driver_means_the_axis_is_ruled_out(self) -> None:
        """
        The verdict must follow: a proportional axis is `not_it`, and its statement
        must not name a bucket as though it were a cause.
        """
        d = self._axis("billing_cycle", {"monthly": 0.722, "annual": 0.278},
                       {"monthly": 0.761, "annual": 0.239})
        hypothesis = build_hypothesis(d)
        assert hypothesis.verdict == "not_it"
        assert "monthly" not in hypothesis.statement, (
            "a ruled-out axis must not name its largest bucket as a driver"
        )

    def test_the_statement_reports_the_lift(self) -> None:
        """
        "47% of the gap" and "47% of the gap while being 45% of the base" are
        different claims, and only the second lets the reader check it.
        """
        d = self._axis(
            "plan_type",
            {"standard": 0.455, "basic": 0.222, "premium": 0.323},
            {"standard": 0.472, "basic": 0.376, "premium": 0.152},
        )
        statement = build_hypothesis(d).statement
        assert "basic" in statement
        assert "its share of the base" in statement
        assert "1.6" in statement or "1.7" in statement, (
            f"the lift multiple is not stated: {statement}"
        )
