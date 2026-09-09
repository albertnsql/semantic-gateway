"""
tests/test_diagnostics_windows.py — comparison windows that cannot overlap.

The whole reason this module exists is one failure mode: MetricFlow's time predicate
is inclusive and a monthly-grain metric's bounds are truncated to whole months, so a
window built from raw day arithmetic can silently share a month with the period it is
being compared against. The shared month's movement then cancels and the gap is
understated, with no error anywhere.

The non-overlap tests below are the ones that matter. The rest guard the arithmetic
they depend on.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.diagnostics.windows import (
    asks_about_a_year,
    clamp_to_complete_months,
    default_comparison,
    last_complete_month,
    Window,
    add_months,
    describe_comparison,
    month_end,
    month_start,
    previous_period,
    trailing_months,
    year_over_year,
)


class TestMonthArithmetic:
    def test_add_months_clamps_a_short_month(self) -> None:
        """31 Jan minus one month has no 31st to land on."""
        assert add_months(date(2026, 1, 31), -1) == date(2025, 12, 31)
        assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
        assert add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)  # leap year

    def test_add_months_crosses_year_boundaries(self) -> None:
        assert add_months(date(2026, 2, 15), -3) == date(2025, 11, 15)
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)

    def test_add_months_zero_is_identity(self) -> None:
        assert add_months(date(2026, 6, 15), 0) == date(2026, 6, 15)

    def test_month_bounds(self) -> None:
        assert month_start(date(2026, 6, 17)) == date(2026, 6, 1)
        assert month_end(date(2026, 6, 17)) == date(2026, 6, 30)
        assert month_end(date(2026, 2, 5)) == date(2026, 2, 28)


class TestWindowBasics:
    def test_inverted_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="ends before it starts"):
            Window.of("2026-06-30", "2026-01-01")

    def test_months_spanned_counts_calendar_months_touched(self) -> None:
        """
        NOT days/30. This is the number that decides what a monthly metric reads, and
        the source of the whole overlap problem.
        """
        assert Window.of("2026-05-20", "2026-08-20").months_spanned == 4
        assert Window.of("2026-01-01", "2026-06-30").months_spanned == 6
        assert Window.of("2026-08-01", "2026-08-01").months_spanned == 1

    def test_days_is_inclusive_at_both_ends(self) -> None:
        assert Window.of("2026-01-01", "2026-01-01").days == 1
        assert Window.of("2026-01-01", "2026-01-31").days == 31

    def test_alignment_detection_and_widening(self) -> None:
        assert Window.of("2026-01-01", "2026-06-30").month_aligned
        ragged = Window.of("2026-05-20", "2026-08-20")
        assert not ragged.month_aligned
        assert ragged.align_to_months() == Window.of("2026-05-01", "2026-08-31")

    def test_str_is_iso(self) -> None:
        assert str(Window.of("2026-01-01", "2026-06-30")) == "2026-01-01..2026-06-30"


class TestPreviousPeriodNeverOverlaps:
    """The bug this module was written to prevent."""

    def test_the_ragged_window_case_that_would_overlap(self) -> None:
        """
        2026-05-20..2026-08-20 reads as May..Aug for a monthly metric. Naive day
        arithmetic gives a "previous 3 months" of Feb-17..May-19, which MetricFlow
        widens to Feb..May — sharing May with the target.
        """
        target = Window.of("2026-05-20", "2026-08-20")
        previous = previous_period(target)
        assert previous == Window.of("2026-01-01", "2026-04-30")
        assert not previous.overlaps(target.align_to_months())
        assert previous.months_spanned == target.align_to_months().months_spanned

    def test_clean_half_year(self) -> None:
        target = Window.of("2026-01-01", "2026-06-30")
        assert previous_period(target) == Window.of("2025-07-01", "2025-12-31")

    def test_single_month(self) -> None:
        target = Window.of("2026-08-01", "2026-08-31")
        assert previous_period(target) == Window.of("2026-07-01", "2026-07-31")

    def test_single_day_still_yields_a_whole_previous_month(self) -> None:
        """A monthly metric reads a single day as its whole month."""
        assert previous_period(Window.of("2026-08-14", "2026-08-14")) == Window.of(
            "2026-07-01", "2026-07-31"
        )

    @pytest.mark.parametrize(
        "start, end",
        [
            ("2026-01-01", "2026-01-31"), ("2026-01-15", "2026-03-02"),
            ("2025-11-01", "2026-02-28"), ("2024-02-01", "2024-02-29"),
            ("2026-05-20", "2026-08-20"), ("2026-12-01", "2026-12-31"),
        ],
    )
    def test_never_overlaps_and_never_leaves_a_gap(self, start, end) -> None:
        """
        The two invariants together. A gap would silently exclude a month from both
        sides of the comparison, which is a different way to understate a movement.
        """
        target = Window.of(start, end).align_to_months()
        previous = previous_period(target)
        assert not previous.overlaps(target)
        assert add_months(previous.end, 0).day == month_end(previous.end).day
        # contiguous: the day after the comparison ends is the day the target starts
        assert date.fromordinal(previous.end.toordinal() + 1) == target.start
        assert previous.months_spanned == target.months_spanned

    def test_fine_grain_mode_honours_days(self) -> None:
        """Session- and payment-level metrics are not month-truncated."""
        target = Window.of("2026-06-10", "2026-06-19")  # 10 days
        previous = previous_period(target, monthly_grain=False)
        assert previous == Window.of("2026-05-31", "2026-06-09")
        assert previous.days == target.days
        assert not previous.overlaps(target)


class TestYearOverYear:
    def test_same_months_one_year_back(self) -> None:
        assert year_over_year(Window.of("2026-01-01", "2026-06-30")) == Window.of(
            "2025-01-01", "2025-06-30"
        )

    def test_ragged_input_is_aligned_first(self) -> None:
        assert year_over_year(Window.of("2026-05-20", "2026-08-20")) == Window.of(
            "2025-05-01", "2025-08-31"
        )

    def test_leap_day_does_not_raise(self) -> None:
        assert year_over_year(Window.of("2024-02-01", "2024-02-29")) == Window.of(
            "2023-02-01", "2023-02-28"
        )

    def test_it_never_overlaps_a_window_shorter_than_a_year(self) -> None:
        target = Window.of("2026-01-01", "2026-06-30")
        assert not year_over_year(target).overlaps(target)


class TestTrailingMonths:
    def test_inclusive_includes_the_anchor_month(self) -> None:
        assert trailing_months("2026-08-14", 3) == Window.of("2026-06-01", "2026-08-31")

    def test_exclusive_drops_a_month_still_in_progress(self) -> None:
        """
        On fct_mrr_monthly the current month is structurally churn-only — the spine
        runs to current_date() while cancellations carry a +1 month offset — so the
        anchor month is the single most misleading one to include.
        """
        assert trailing_months("2026-08-14", 3, inclusive=False) == Window.of(
            "2026-05-01", "2026-07-31"
        )

    def test_it_crosses_a_year_boundary(self) -> None:
        assert trailing_months("2026-02-10", 4) == Window.of("2025-11-01", "2026-02-28")

    def test_one_month(self) -> None:
        assert trailing_months("2026-08-01", 1) == Window.of("2026-08-01", "2026-08-31")

    def test_zero_months_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            trailing_months("2026-08-01", 0)


class TestDescribeComparison:
    """A diagnosis that does not name its baseline is unreadable."""

    def test_it_recognises_the_preceding_period(self) -> None:
        target = Window.of("2026-01-01", "2026-06-30")
        assert "preceding 6 months" in describe_comparison(target, previous_period(target))

    def test_it_recognises_year_over_year(self) -> None:
        target = Window.of("2026-01-01", "2026-06-30")
        assert "a year earlier" in describe_comparison(target, year_over_year(target))


    def test_it_pluralises_correctly(self) -> None:
        """
        "the preceding 1 months" reached a live answer. The year-over-year branch is
        the opposite case: a hyphenated compound adjective stays singular, so it is
        "the same 6-month period", never "6-months".
        """
        one = Window.of("2026-06-01", "2026-06-30")
        assert describe_comparison(one, previous_period(one)) == "the preceding month"
        assert "1-month period" in describe_comparison(one, year_over_year(one))

        six = Window.of("2026-01-01", "2026-06-30")
        assert describe_comparison(six, previous_period(six)) == "the preceding 6 months"
        assert "6-month period" in describe_comparison(six, year_over_year(six))
        assert "6-months" not in describe_comparison(six, year_over_year(six))

    def test_an_arbitrary_comparison_is_spelled_out(self) -> None:
        described = describe_comparison(
            Window.of("2026-01-01", "2026-06-30"), Window.of("2024-03-01", "2024-04-30")
        )
        assert "2024-03-01..2024-04-30" in described


class TestTimeRangeHandoff:
    def test_it_produces_the_gateway_time_range(self) -> None:
        tr = Window.of("2026-01-01", "2026-06-30").as_time_range()
        assert (tr.start_date, tr.end_date) == ("2026-01-01", "2026-06-30")

    def test_the_module_stays_stdlib_only_until_asked(self) -> None:
        """
        `as_time_range()` imports lazily so this module keeps the same cheap import
        profile as analysis.py — importable with no gateway dependencies.
        """
        import subprocess
        import sys

        code = (
            "import sys, core.diagnostics.windows;"
            "print(','.join(m for m in ('openai','duckdb','metricflow','config') "
            "if m in sys.modules))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, proc.stderr[-400:]
        assert not proc.stdout.strip(), f"windows.py now pulls in: {proc.stdout.strip()}"


#: Fixed "now" for every case below. 2026-09-09 makes August the last complete
#: month, which is also where the event facts genuinely end after the 2026-08 load.
_TODAY = date(2026, 9, 9)


class TestLikeForLikeYearComparison:
    """
    "This year versus last year" must compare COMPLETE months on both sides.

    Two independent defects, and each hid the other. Asked on 2026-09-09:

        was  target 2026-01-01..2026-09-09  vs  2025-04-01..2025-12-31
        now  target 2026-01-01..2026-08-31  vs  2025-01-01..2025-08-31

    The old comparison set January-September against April-December. Nine months on
    each side, so no length check caught it, and it is not a year-over-year
    comparison at all -- it straddles the seasonality that comparing against a year
    earlier exists to control for.

    Separately, a full-year target (`2026-01-01..2026-12-31`) read eight months of
    real data against twelve, so the target lost a third of its volume to a window
    boundary and every metric looked collapsed. The dashboard hit the same class of
    bug: CLAUDE.md records `revenue_kpi` comparing six months of 2026 against SEVEN
    of 2025 and reporting +81.7% where like-for-like was +119.0%.
    """

    def test_the_last_complete_month_is_the_previous_one(self) -> None:
        assert last_complete_month(_TODAY) == date(2026, 8, 31)
        # January: the last complete month is in the previous year.
        assert last_complete_month(date(2026, 1, 20)) == date(2025, 12, 31)

    @pytest.mark.parametrize("start,end", [
        ("2026-01-01", "2026-12-31"),   # the whole year, asked mid-year
        ("2026-01-01", "2026-09-09"),   # year to date, ending today
        ("2026-01-01", "2026-08-31"),   # year to date, already month-aligned
    ])
    def test_every_shape_of_this_year_converges(self, start, end) -> None:
        """
        The LLM emits all three for the same question, so all three must land on the
        same windows or the answer depends on phrasing.
        """
        target = clamp_to_complete_months(Window.of(start, end), today=_TODAY)
        assert target == Window.of("2026-01-01", "2026-08-31")
        assert default_comparison(target, today=_TODAY) == Window.of(
            "2025-01-01", "2025-08-31"
        )

    def test_both_sides_have_the_same_month_count(self) -> None:
        target = clamp_to_complete_months(
            Window.of("2026-01-01", "2026-12-31"), today=_TODAY
        )
        comparison = default_comparison(target, today=_TODAY)
        assert target.months_spanned == comparison.months_spanned == 8

    def test_the_users_five_month_example(self) -> None:
        """The case as stated: five complete months against the same five."""
        today = date(2026, 6, 15)          # May is the last complete month
        target = clamp_to_complete_months(
            Window.of("2026-01-01", "2026-12-31"), today=today
        )
        assert target == Window.of("2026-01-01", "2026-05-31")
        assert target.months_spanned == 5
        assert default_comparison(target, today=today) == Window.of(
            "2025-01-01", "2025-05-31"
        )

    def test_a_completed_past_year_keeps_all_twelve_months(self) -> None:
        """A trim must never shorten a year that has already finished."""
        window = Window.of("2025-01-01", "2025-12-31")
        assert clamp_to_complete_months(window, today=_TODAY) == window
        assert default_comparison(window, today=_TODAY) == Window.of(
            "2024-01-01", "2024-12-31"
        )

    def test_an_in_progress_month_is_still_answered_as_asked(self) -> None:
        """
        "Why did churn spike in September 2026", asked on 2026-09-09. Clamping this
        would end the window before it starts. A single month is a legitimate
        question about an in-progress month.
        """
        window = Window.of("2026-09-01", "2026-09-30")
        assert clamp_to_complete_months(window, today=_TODAY) == window

    def test_january_alone_does_not_make_it_a_year_question(self) -> None:
        """
        The mistake an earlier version of the predicate made. `2026-01-01..2026-06-30`
        is a plain H1 question; treating it as a year silently switched it from
        "against H2 2025" to "against H1 2025" and broke four calibration fixtures.
        """
        h1 = Window.of("2026-01-01", "2026-06-30")
        assert not asks_about_a_year(h1, today=_TODAY)
        assert clamp_to_complete_months(h1, today=_TODAY) == h1
        assert default_comparison(h1, today=_TODAY) == Window.of(
            "2025-07-01", "2025-12-31"
        )

    def test_a_mid_year_stretch_keeps_the_preceding_period(self) -> None:
        w = Window.of("2026-03-01", "2026-11-30")
        assert not asks_about_a_year(w, today=_TODAY)
        assert default_comparison(w, today=_TODAY) == previous_period(w)

    def test_a_year_with_no_complete_month_yet_is_left_alone(self) -> None:
        """Asked in January: there is nothing to trim to, so do not build an empty window."""
        window = Window.of("2026-01-01", "2026-12-31")
        assert clamp_to_complete_months(window, today=date(2026, 1, 10)) == window

    def test_the_comparison_never_overlaps_the_target(self) -> None:
        for end in ("2026-12-31", "2026-09-09", "2026-08-31"):
            target = clamp_to_complete_months(Window.of("2026-01-01", end), today=_TODAY)
            assert not target.overlaps(default_comparison(target, today=_TODAY))
