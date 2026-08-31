"""
core/diagnostics/windows.py — building the comparison window without overlapping it.

`analysis.decompose()` takes a target and a comparison. Nothing constructed the
comparison *window*, and doing it naively walks into a documented trap.

MetricFlow's time predicate is **inclusive**, and for a monthly-grain metric the
bounds are truncated to whole months. Confirmed against the CLI (CLAUDE.md, template
cache section):

    mf 2026-05-20 .. 2026-08-20  ->  BETWEEN '2026-05-01' AND '2026-08-31'
    mf 2026-08-01 .. 2026-08-01  ->  BETWEEN '2026-08-01' AND '2026-08-31'
    mf 2026-01-01 .. 2026-12-31  ->  BETWEEN '2026-01-01' AND '2026-12-31'

So a 3-month-looking window of 2026-05-20..2026-08-20 actually reads **four** months.
Subtract 3 months of days from it and the "previous period" spans Feb..May — which
**overlaps the target's May**. The same month lands on both sides of the comparison,
its movement cancels, and the gap is understated with no error anywhere.

This is not hypothetical for this project: the same inclusive-bound behaviour once
made a single-month churn_rate read **8.7%** where the true monthly rate was 4.4%,
because the window spilled into `fct_mrr_monthly`'s trailing churn-only period.

`previous_period(..., monthly_grain=True)` therefore snaps to whole months FIRST and
then steps back whole months, which cannot overlap by construction. Every function
here is stdlib-only, so this module stays as cheap to test as `analysis.py`.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

_ISO = "%Y-%m-%d"


def _parse(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def add_months(anchor: date, months: int) -> date:
    """
    Shift by whole months, clamping the day to the target month's length.

    Clamping matters: 31 January minus one month has no 31st to land on, and
    `timedelta(days=30)` arithmetic drifts a little every time it is applied, which
    is how a "previous 6 months" quietly becomes 5 months and 27 days.
    """
    total = (anchor.year * 12 + anchor.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_start(anchor: date) -> date:
    return anchor.replace(day=1)


def month_end(anchor: date) -> date:
    return anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])


@dataclass(frozen=True)
class Window:
    """A closed date interval, inclusive at both ends — matching MetricFlow."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window ends before it starts: {self.start}..{self.end}")

    @classmethod
    def of(cls, start: str | date, end: str | date) -> "Window":
        return cls(_parse(start), _parse(end))

    @property
    def months_spanned(self) -> int:
        """
        Distinct calendar months the window touches — what a monthly metric reads.

        Deliberately not "length in days / 30": 2026-05-20..2026-08-20 touches four
        months, and that is the number that determines what MetricFlow returns.
        """
        return (self.end.year - self.start.year) * 12 + (self.end.month - self.start.month) + 1

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def month_aligned(self) -> bool:
        return self.start == month_start(self.start) and self.end == month_end(self.end)

    def align_to_months(self) -> "Window":
        """Widen to whole months, which is what a monthly-grain metric will read anyway."""
        return Window(month_start(self.start), month_end(self.end))

    def overlaps(self, other: "Window") -> bool:
        return self.start <= other.end and other.start <= self.end

    def as_time_range(self):
        """
        Build the gateway's `TimeRange`. Imported lazily so this module keeps its
        stdlib-only import profile for tests.
        """
        from core.intent_extractor import TimeRange

        return TimeRange(
            start_date=self.start.strftime(_ISO), end_date=self.end.strftime(_ISO)
        )

    def __str__(self) -> str:
        return f"{self.start.strftime(_ISO)}..{self.end.strftime(_ISO)}"


def previous_period(window: Window, *, monthly_grain: bool = True) -> Window:
    """
    The window of equal length immediately before *window*, never overlapping it.

    With ``monthly_grain`` (the default, and correct for every metric on
    `fct_mrr_monthly`) the target is first widened to whole months and the result is
    the same number of whole months directly before it. That is the only way to
    guarantee non-overlap, because MetricFlow will widen the target to whole months
    whether or not the caller did.

    Set ``monthly_grain=False`` only for a metric whose grain is genuinely finer —
    session or payment level — where day boundaries are honoured as given.
    """
    if monthly_grain:
        aligned = window.align_to_months()
        months = aligned.months_spanned
        start = add_months(aligned.start, -months)
        return Window(month_start(start), month_end(add_months(aligned.end, -months)))

    length = window.days
    end = date.fromordinal(window.start.toordinal() - 1)
    return Window(date.fromordinal(end.toordinal() - length + 1), end)


def year_over_year(window: Window, *, monthly_grain: bool = True) -> Window:
    """
    The same window one year earlier.

    Often the better comparison than the immediately preceding period: a subscription
    business has seasonality, and "December against November" attributes a seasonal
    swing to whatever segment happens to be biggest. Which comparison to use is a
    judgment for the planner — this module only builds both correctly.
    """
    if monthly_grain:
        aligned = window.align_to_months()
        return Window(
            month_start(add_months(aligned.start, -12)),
            month_end(add_months(aligned.end, -12)),
        )
    return Window(add_months(window.start, -12), add_months(window.end, -12))


def trailing_months(anchor: str | date, months: int, *, inclusive: bool = True) -> Window:
    """
    The *months* whole calendar months ending at *anchor*'s month.

    ``inclusive`` includes the anchor's own month. Pass False to exclude it when that
    month is still in progress — a partial month compared against complete ones reads
    as a collapse, and on `fct_mrr_monthly` the current month is structurally
    churn-only (its date spine runs to `current_date()` while cancellations carry a
    +1 month offset), so the anchor month is the single most misleading one to include.
    """
    if months < 1:
        raise ValueError("months must be >= 1")
    anchor_date = _parse(anchor)
    last = month_end(anchor_date if inclusive else add_months(anchor_date, -1))
    first = month_start(add_months(last, -(months - 1)))
    return Window(first, last)


def describe_comparison(target: Window, comparison: Window) -> str:
    """
    One phrase naming what was compared, for the answer.

    A diagnosis that does not state its baseline is unreadable: "revenue is down 18%"
    means nothing without "against the previous six months".
    """
    months = target.months_spanned
    if comparison.months_spanned == months:
        if year_over_year(target) == comparison:
            # Hyphenated compound adjective: always singular. "6-month period",
            # never "6-months period".
            return f"the same {months}-month period a year earlier"
        if previous_period(target) == comparison:
            # "the preceding 1 months" appeared in a live answer.
            return "the preceding month" if months == 1 else f"the preceding {months} months"
    return f"{comparison} (target {target})"
