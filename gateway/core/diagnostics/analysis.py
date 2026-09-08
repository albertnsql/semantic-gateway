"""
core/diagnostics/analysis.py — the arithmetic behind a causal claim.

This module is why the diagnostic path is allowed to say "because" at all. The
narrative prompt forbids causal language because the narrator sees values without
semantics and will assert a cause from three numbers. The answer is not to trust it
more; it is to compute the attribution here, deterministically, and let synthesis
only restate what these functions produced.

So: **no LLM call, and no gateway import.** Pure functions over `state.py` types.
Every claim the agent makes about how much of a gap something explains comes from
`decompose()`, which is exact by construction and asserted to be in the tests.

Two decompositions, picked by whether weights are supplied:

*Additive* — total_revenue, total_sessions, churned_subscribers. The total is the
sum of its buckets, so a bucket's contribution is simply its own change:

    gap = sum_i (v_i^T - v_i^C)

*Weighted* — churn_rate, engagement_rate, avg_watch_time, and any per-unit rate.
The total is a weighted mean, so it can move two ways that mean entirely different
things, and conflating them is the classic mis-read:

    total = sum_i (w_i * r_i)
    mix_i  = (w_i^T - w_i^C) * r_i^C     the composition shifted
    rate_i = w_i^T * (r_i^T - r_i^C)     performance within the bucket shifted

    mix_i + rate_i sums exactly to the gap (see test_weighted_effects_are_exact).

"Revenue per user fell because we sold more cheap plans" and "…because each plan
earns less" are different problems with different owners. A single number cannot
distinguish them; these two can.

Weights normally come from a second probe — the ratio's denominator — which is why
`driver_graph.yml` carries `probe_both_sides_of_a_ratio: true` and why `churn_rate`
lists `total_subscribers` among its drivers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from core.diagnostics.state import (
    Bucket,
    Confidence,
    Contribution,
    Decomposition,
    Hypothesis,
    Presence,
    Verdict,
)

# A share of the gap below this is noise dressed as a finding.
DEFAULT_MIN_SHARE = 0.05
# ...and a share is meaningless when the gap itself is ~0, so an absolute floor is
# needed too: "explains 80% of the gap" reads very differently for $40,000 and $3.
DEFAULT_MIN_ABSOLUTE = 1e-9
# At or above this, one dimension is the answer rather than a part of it.
DEFAULT_STRONG_SHARE = 0.60
#: How much MORE than its own size a bucket must carry before it counts as driving
#: the movement. A bucket holding 45% of the base and 47% of the gap has a lift of
#: 1.04 -- it moved exactly in proportion to how big it is, which is arithmetic, not
#: a finding. Without this gate the largest bucket almost always wins, and the
#: largest bucket is the least informative answer available.
#:
#: Calibrated on the June-2026 churn case, where the two readings are unambiguous::
#:
#:     bucket                 base    gap    lift
#:     plan_type=standard    45.5%  47.2%   1.04x   <- named as the cause, wrongly
#:     plan_type=basic       22.2%  37.6%   1.69x   <- the actual driver
#:     billing_cycle=monthly 72.2%  76.1%   1.05x   <- named as the cause, wrongly
#:
#: CLAUDE.md already records the business fact this recovers: "standard is the
#: largest plan, so it wins on count while basic is worst on rate."
#:
#: 1.25 sits clear of proportional noise (1.04-1.05) with margin below the real
#: signal (1.69). Low-cardinality axes are the reason it matters most: with two
#: buckets one MUST hold >= 50% of the gap unless they are near-balanced, so a
#: share-only test grades `billing_cycle` as explaining almost any movement.
DEFAULT_MIN_LIFT = 1.25
#: A bucket must carry at least this much of the gap to be named its DRIVER. That
#: is a higher bar than DEFAULT_MIN_SHARE (0.05), which only decides whether a
#: bucket is worth mentioning — being 5% of a movement and being its cause are
#: different claims.
#:
#: Both floors are needed, and each without the other misfires on real data:
#:
#:   * lift alone, at the 5% floor: revenue H1-2026 grew +181,599 and `country=DE`
#:     carries 7.5% of that at 1.31x lift, so DE would be reported as the driver of
#:     a movement it accounts for a fourteenth of.
#:   * share alone: `billing_cycle=monthly` carries 76.1% of the June churn gap at
#:     1.05x lift — proportional to its 72.2% of the base, and not a finding.
#:
#: Checked against every real case available:
#:
#:     case                            share   lift   driver?
#:     churn Jun  plan_type=basic      37.6%  1.69x   yes  <- the real story
#:     churn Jun  plan_type=standard   47.2%  1.04x   no   (merely the biggest)
#:     churn Jun  billing_cycle=monthly 76.1% 1.05x   no   (merely the biggest)
#:     revenue H1 plan_type=premium    50.7%  1.20x   no   (below the lift floor)
#:     revenue H1 country=DE            7.5%  1.31x   no   (below the share floor)
#:     revenue H1 payment_method=card  26.6%  1.08x   no   (proportional)
#:
#: which preserves the deliberately calibrated "revenue growth is broad-based"
#: verdict while recovering the churn finding the old rule inverted.
DEFAULT_DRIVER_MIN_SHARE = 0.25


def _to_map(buckets: Iterable[Bucket]) -> dict[str | None, Bucket]:
    """Index buckets by label, summing duplicates rather than silently keeping one."""
    out: dict[str | None, Bucket] = {}
    for b in buckets:
        existing = out.get(b.label)
        if existing is None:
            out[b.label] = b
            continue
        weight = None
        if existing.weight is not None or b.weight is not None:
            weight = (existing.weight or 0.0) + (b.weight or 0.0)
        out[b.label] = Bucket(b.label, existing.value + b.value, weight)
    return out


def _presence(in_target: bool, in_comparison: bool) -> Presence:
    if in_target and in_comparison:
        return "both"
    return "target_only" if in_target else "comparison_only"


def _weighted_total(buckets: Iterable[Bucket]) -> float:
    """sum(w * r). Buckets with no weight contribute nothing to a weighted total."""
    return sum((b.weight or 0.0) * b.value for b in buckets)


def decompose(
    dimension: str,
    target: Sequence[Bucket],
    comparison: Sequence[Bucket],
) -> Decomposition:
    """
    Attribute the gap between two sides to the buckets of one dimension.

    Weighted decomposition is used when EVERY bucket on both sides carries a weight;
    otherwise additive. Mixing the two would produce a total that matches neither, so
    a partially-weighted input degrades to additive and says so in ``note``.

    Buckets present on only one side are kept, with the missing side treated as zero
    and `presence` recording it. That keeps the decomposition exact and surfaces a
    launched-or-retired segment instead of hiding it.

    Contributions come back sorted by absolute delta, largest first.
    """
    t_map = _to_map(target)
    c_map = _to_map(comparison)
    labels = list(t_map) + [k for k in c_map if k not in t_map]

    all_buckets = list(t_map.values()) + list(c_map.values())
    weighted = bool(all_buckets) and all(b.weight is not None for b in all_buckets)
    note = ""
    if not weighted and any(b.weight is not None for b in all_buckets):
        note = (
            "weights present on some buckets but not all - fell back to additive, "
            "because a partly-weighted total matches neither decomposition"
        )

    if weighted:
        t_total = _weighted_total(t_map.values())
        c_total = _weighted_total(c_map.values())
    else:
        t_total = sum(b.value for b in t_map.values())
        c_total = sum(b.value for b in c_map.values())

    gap = t_total - c_total

    contributions: list[Contribution] = []
    for label in labels:
        tb = t_map.get(label)
        cb = c_map.get(label)
        presence = _presence(tb is not None, cb is not None)

        if weighted:
            wt = tb.weight if tb and tb.weight is not None else 0.0
            wc = cb.weight if cb and cb.weight is not None else 0.0
            rt = tb.value if tb else 0.0
            rc = cb.value if cb else 0.0
            mix = (wt - wc) * rc
            rate = wt * (rt - rc)
            delta = mix + rate
            t_amount, c_amount = wt * rt, wc * rc
        else:
            t_amount = tb.value if tb else 0.0
            c_amount = cb.value if cb else 0.0
            delta = t_amount - c_amount
            mix = rate = None

        contributions.append(
            Contribution(
                label=label,
                target=t_amount,
                comparison=c_amount,
                delta=delta,
                share=(delta / gap) if gap else 0.0,
                presence=presence,
                mix_effect=mix,
                rate_effect=rate,
            )
        )

    contributions.sort(key=lambda c: abs(c.delta), reverse=True)
    residual = gap - sum(c.delta for c in contributions)

    return Decomposition(
        dimension=dimension,
        target_total=t_total,
        comparison_total=c_total,
        gap=gap,
        contributions=contributions,
        residual=residual,
        weighted=weighted,
        note=note,
    )


def concentration(decomposition: Decomposition) -> tuple[float, int]:
    """
    How concentrated the gap is: (share held by the biggest bucket, buckets to 80%).

    "The shortfall is entirely in Germany" and "it is spread evenly across fifteen
    countries" are different findings, and the second is usually the more important
    one because it rules a segment story out.

    Only same-signed contributions count toward the 80%: with offsetting movements a
    cumulative share can exceed 1 and then fall back, which would make "buckets to
    80%" meaningless.
    """
    if not decomposition.contributions or not decomposition.gap:
        return 0.0, 0

    same_sign = [
        c for c in decomposition.contributions
        if (c.delta > 0) == (decomposition.gap > 0) and c.delta
    ]
    if not same_sign:
        return 0.0, 0

    top_share = abs(same_sign[0].delta / decomposition.gap)
    cumulative, count = 0.0, 0
    for c in same_sign:
        cumulative += abs(c.delta / decomposition.gap)
        count += 1
        if cumulative >= 0.80:
            break
    return top_share, count


def bucket_lift(contribution: Contribution, comparison_total: float) -> float | None:
    """How much more (or less) of the gap a bucket carries than its size implies.

    ``lift = share of the gap / share of the comparison-period base``

    1.0 means proportional — the bucket moved exactly as much as being that big
    would predict. Above 1.0 it is over-contributing, which is the only thing that
    makes it a candidate cause.

    Returns None when the bucket has no baseline at all (it appears only in the
    target window). That is not a lift of infinity to be reported as a huge number;
    it is a categorically different finding — a segment that did not exist before —
    and callers treat it as over-contributing without inventing a ratio.
    """
    if not comparison_total:
        return None
    base_share = contribution.comparison / comparison_total
    if not base_share:
        return None
    gap_share = contribution.share
    return abs(gap_share) / abs(base_share)


def leading_driver(
    decomposition: Decomposition,
    *,
    min_share: float = DEFAULT_DRIVER_MIN_SHARE,
    min_lift: float = DEFAULT_MIN_LIFT,
) -> Contribution | None:
    """The bucket that actually drives the movement, or None if none does.

    Two conditions, and dropping either one produces a bad answer:

    * **material** — it carries at least *min_share* of the gap. Without this a
      0.1% bucket that doubled has a huge lift and would be reported as the cause
      of a movement it cannot account for.
    * **over-contributing** — lift >= *min_lift*, i.e. it carries more of the gap
      than its size implies. Without this the biggest bucket wins by construction:
      `Decomposition.top` is simply `contributions[0]`, sorted by absolute delta.

    Among the buckets that qualify, the largest absolute delta wins, so the answer
    still names the segment carrying the most movement — just not one that is
    merely large.

    Only same-signed buckets are eligible: a bucket moving against the gap is not
    driving it.
    """
    if not decomposition.contributions or not decomposition.gap:
        return None

    aligned = [
        c for c in decomposition.contributions
        if (c.delta > 0) == (decomposition.gap > 0) and c.delta
    ]
    qualified = []
    for c in aligned:
        if abs(c.share) < min_share:
            continue
        lift = bucket_lift(c, decomposition.comparison_total)
        # None = no baseline, i.e. a segment new in the target window. Genuinely
        # over-contributing, and there is no ratio to compare.
        if lift is None or lift >= min_lift:
            qualified.append(c)
    if not qualified:
        return None
    return max(qualified, key=lambda c: abs(c.delta))


def explained_share(decomposition: Decomposition) -> float:
    """
    Fraction of the gap the same-signed buckets account for.

    Not simply 1.0: buckets move in both directions, so a dimension where +120 and
    -20 net to +100 explains the gap differently from one where every bucket moved
    +10. This measures the pull in the gap's own direction.
    """
    if not decomposition.gap:
        return 0.0
    aligned = sum(
        c.delta for c in decomposition.contributions
        if (c.delta > 0) == (decomposition.gap > 0)
    )
    return abs(aligned / decomposition.gap)


def clears_floor(
    decomposition: Decomposition,
    min_share: float = DEFAULT_MIN_SHARE,
    min_absolute: float = DEFAULT_MIN_ABSOLUTE,
) -> bool:
    """
    Whether this decomposition is worth reporting at all.

    Both gates matter. Share alone lets a rounding error explain 90% of a gap of
    $0.02; absolute alone lets a large number that is 1% of an enormous gap look
    important.
    """
    if not decomposition.exact:
        return False
    if abs(decomposition.gap) < min_absolute:
        return False
    top_share, _ = concentration(decomposition)
    return top_share >= min_share


def _fmt(value: float) -> str:
    """Compact number for a statement. No currency symbol - the metric decides that."""
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.0f}"
    if magnitude >= 1:
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:.4g}"


def build_hypothesis(
    decomposition: Decomposition,
    evidence: Sequence[str] = (),
    confidence: Confidence = "contribution",
    min_share: float = DEFAULT_MIN_SHARE,
    strong_share: float = DEFAULT_STRONG_SHARE,
) -> Hypothesis:
    """
    Turn a decomposition into a ranked, plainly-worded candidate explanation.

    The wording is produced HERE, not by a model. Synthesis may re-phrase it, but the
    numbers and the strength claim originate in arithmetic, which is what makes the
    citation check in synthesis meaningful rather than decorative.

    `inconclusive` is returned when the arithmetic itself cannot be trusted -- a
    non-exact decomposition or a gap of ~0 -- and is deliberately distinct from
    `not_it`, which is a real finding.
    """
    dim = decomposition.dimension
    share = explained_share(decomposition)
    top_share, to_eighty = concentration(decomposition)
    top = decomposition.top

    if not decomposition.exact:
        return Hypothesis(
            dimension=dim,
            statement=(
                f"{dim} could not be decomposed reliably: the buckets leave a "
                f"residual of {_fmt(decomposition.residual)} against a gap of "
                f"{_fmt(decomposition.gap)}. Probably an inconsistent comparison."
            ),
            confidence=confidence,
            verdict="inconclusive",
            explained_share=0.0,
            evidence=list(evidence),
            decomposition=decomposition,
        )

    if abs(decomposition.gap) < DEFAULT_MIN_ABSOLUTE:
        return Hypothesis(
            dimension=dim,
            statement=f"There is no material gap to explain by {dim}.",
            confidence=confidence,
            verdict="inconclusive",
            explained_share=0.0,
            evidence=list(evidence),
            decomposition=decomposition,
        )

    # The bucket that drives the gap, which is NOT `decomposition.top` — that is
    # simply the largest, and the largest bucket carries the most movement almost
    # by definition. A live churn answer named `plan_type = standard` (45.5% of the
    # base, 47.2% of the gap, lift 1.04) while `basic` (22.2% base, 37.6% gap, lift
    # 1.69) was the real story. See leading_driver().
    # Deliberately NOT `min_share=min_share`: that argument is the REPORTING floor
    # (5%), and a bucket carrying 5% of a movement is not its cause. leading_driver
    # applies DEFAULT_DRIVER_MIN_SHARE instead.
    driver = leading_driver(decomposition)
    if driver is None:
        biggest = f"{top_share:.1%}" if top is not None else "0.0%"
        return Hypothesis(
            dimension=dim,
            statement=(
                f"{dim} does not explain the gap - no value carries meaningfully "
                f"more of it than its own size implies (largest single share "
                f"{biggest})."
            ),
            confidence=confidence,
            verdict="not_it",
            explained_share=share,
            evidence=list(evidence),
            decomposition=decomposition,
        )
    top = driver
    top_share = abs(driver.delta / decomposition.gap)

    label = "(not set)" if top.label is None else top.label
    direction = "lower" if decomposition.gap < 0 else "higher"
    verdict: Verdict = "explains" if top_share >= strong_share else "partial"

    parts = [
        f"{dim} = {label} accounts for {top_share:.1%} of the gap "
        f"({_fmt(top.delta)} of {_fmt(decomposition.gap)})"
    ]
    # Stated explicitly, because "47% of the gap" and "47% of the gap while being
    # 45% of the base" are different claims and only the second is checkable by
    # the reader.
    lift = bucket_lift(top, decomposition.comparison_total)
    if lift is not None:
        base_share = abs(top.comparison / decomposition.comparison_total)
        parts.append(
            f"{lift:.2f}x its share of the base ({base_share:.1%})"
        )
    else:
        parts.append("a segment with no presence in the comparison period")
    if to_eighty > 1:
        parts.append(f"{to_eighty} values are needed to reach 80%, so it is spread")
    if decomposition.weighted and top.mix_effect is not None:
        # The distinction the weighted decomposition exists to make.
        bigger = "composition" if abs(top.mix_effect) >= abs(top.rate_effect or 0) else "within-bucket rate"
        parts.append(
            f"driven mainly by {bigger} (mix {_fmt(top.mix_effect)}, "
            f"rate {_fmt(top.rate_effect or 0.0)})"
        )
    if top.presence != "both":
        side = "only in the target period" if top.presence == "target_only" else "only in the comparison"
        parts.append(f"note: this value appears {side}")
    if top.is_null_bucket:
        parts.append(
            "this is the unattributed bucket - it may be a data gap rather than a "
            "segment"
        )

    return Hypothesis(
        dimension=dim,
        statement=f"The metric is {direction}: " + "; ".join(parts) + ".",
        confidence=confidence,
        verdict=verdict,
        explained_share=share,
        evidence=list(evidence),
        decomposition=decomposition,
    )


#: Above this, one dimension value leads clearly enough to be named as the story.
#: Below it on EVERY axis, the movement is not attributable to any single value.
DEFAULT_DOMINANT_SHARE = 0.50


#: A comparison window this far from recent trend is not a fair baseline. 20% is a
#: judgment call, calibrated so the May-2026 case (-41%) is flagged and ordinary
#: month-to-month wobble is not.
DEFAULT_BASELINE_TOLERANCE = 0.20


@dataclass(frozen=True)
class BaselineCheck:
    """
    Whether the comparison window was typical of recent history.

    A gap is only as meaningful as what it is measured against, and the default
    previous-period comparison has no idea whether that period was normal.

    The case this exists for: a live churn diagnosis reported **+163.7%** for June
    2026 against May. Both figures were correct — but May was **41% below** the
    trailing 12-month average, so the comparison maximised the apparent jump. Against
    trend, June is **+55%**. The answer was not wrong; it was missing the one fact a
    reader needed to size it.

    Swapping the default baseline for year-over-year would not fix this — it would
    just move the arbitrariness, and on a growing business YoY shows growth for
    everything. Making the baseline's representativeness visible is the honest fix,
    and it costs one extra probe.
    """

    comparison_per_month: float
    trend_per_month: float
    trend_months: int
    deviation: float          # signed: comparison vs trend, as a fraction
    representative: bool
    trend_relative_gap: float | None = None   # target vs trend, when computable

    @property
    def direction(self) -> str:
        return "below" if self.deviation < 0 else "above"


def baseline_representativeness(
    comparison_value: float,
    trend_monthly: Sequence[float],
    target_value: float | None = None,
    tolerance: float = DEFAULT_BASELINE_TOLERANCE,
) -> BaselineCheck | None:
    """
    Judge a SINGLE-MONTH baseline against the mean of a per-month trend series.

    Takes the trend as a list of monthly values rather than one aggregate, because
    aggregating a ratio over a long window does not give a rate. churn_rate over
    2025-06..2026-05 returns 0.206 — the share of all subscribers who churned that
    year, since the denominator is a distinct count over the whole window. Dividing
    by 12 produced 1.72% against a true monthly mean of 2.61%, and the check called a
    41%-below-average baseline "representative". Averaging monthly values is right for
    both a ratio and a sum, so it needs no per-metric special-casing.

    Deliberately scoped to a one-month comparison. That is where the problem bites —
    a 6-month baseline is already averaged — and it keeps the arithmetic exactly
    correct instead of approximately correct over an arbitrary span. Returns None
    otherwise, and None when the series is empty or its mean is zero: "could not tell"
    must not render as "was typical".
    """
    values = [v for v in trend_monthly if v is not None]
    if len(values) < 2:
        return None

    trend_per_month = sum(values) / len(values)
    if not trend_per_month:
        return None

    deviation = (comparison_value - trend_per_month) / abs(trend_per_month)
    trend_relative = None
    if target_value is not None:
        trend_relative = (target_value - trend_per_month) / abs(trend_per_month)

    return BaselineCheck(
        comparison_per_month=comparison_value,
        trend_per_month=trend_per_month,
        trend_months=len(values),
        deviation=deviation,
        representative=abs(deviation) <= tolerance,
        trend_relative_gap=trend_relative,
    )


def is_broad_based(
    hypotheses: Sequence[Hypothesis],
    *,
    dominant_share: float = DEFAULT_DOMINANT_SHARE,
) -> bool:
    """
    True when no single value on any examined axis leads the movement.

    Reporting three weak partials is honest and useless. A live `total_revenue` run
    produced exactly that::

        plan_type = standard accounts for 43.6% of the gap ... so it is spread
        country = US accounts for 39.3% of the gap ... so it is spread
        acquisition_channel = paid_search accounts for 27.3% ... so it is spread

    An analyst reading those concludes "growth is broad-based, no single driver" —
    which is a finding, and is what should be SAID.

    The test is the TOP SHARE on each axis, not buckets-to-80%. That was the first
    attempt and it was wrong: buckets-to-80% is not comparable across axes of
    different cardinality. Two of three plan types is proportionally the same as ten
    of fifteen countries, so a `to_eighty < 3` gate disqualified the revenue case on
    plan_type alone and the verdict never fired on the very output that motivated it.
    Top share is cardinality-invariant.

    Calibrated against both live cases:

    ===============  ==========================  =============
    run              top shares                  verdict
    ===============  ==========================  =============
    total_revenue    43.6%, 39.3%, 27.3%         broad-based
    churn_rate       57.6%, 49.0%, 35.1%         plan_type leads
    ===============  ==========================  =============

    At least two axes must have been examined — one slice says nothing about breadth.

    `inconclusive` hypotheses are excluded, and that guard is load-bearing rather
    than tidy: `concentration()` returns a top share of 0.0 when the gap is ~0, which
    is below any threshold, so a metric that did not move AT ALL would otherwise be
    reported as "broad-based". "Nothing happened" and "it moved everywhere" are
    opposite findings.
    """
    graded = [
        h for h in hypotheses
        if h.decomposition is not None and h.verdict != "inconclusive"
    ]
    if len(graded) < 2:
        return False

    # One condition: no axis has an over-contributing driver.
    #
    # A `top_share < dominant_share` ceiling used to sit alongside this and has been
    # REMOVED, because the two disagree and the share test is the weaker one. On
    # measured revenue growth, `plan_type = premium` holds 50.7% of the gap while
    # being 42.1% of the base — a lift of 1.20, i.e. essentially proportional and
    # not a finding — yet 50.7% trips a 50% share ceiling. Keeping both meant the
    # cruder test overrode the better one and the verdict flipped to "concentrated"
    # on the strength of a bucket merely being large.
    #
    # `dominant_share` is retained in the signature and no longer consulted, so
    # existing callers keep working; the concentration threshold still governs
    # `build_hypothesis`'s explains/partial split, which is a different question.
    return all(leading_driver(h.decomposition) is None for h in graded)


def rank_hypotheses(hypotheses: Iterable[Hypothesis]) -> list[Hypothesis]:
    """
    Order candidates for the answer: strongest contribution first.

    Contribution always outranks association regardless of size, because a large
    correlation is still not an explanation and presenting it above a smaller
    arithmetic attribution would invert the confidence tiers the whole design rests
    on. Ruled-out and inconclusive results sort last but are NOT dropped -- "I
    checked this and it is not the cause" belongs in the answer.
    """
    tier = {"contribution": 0, "association": 1}
    strength = {"explains": 0, "partial": 1, "not_it": 2, "inconclusive": 3}
    return sorted(
        hypotheses,
        key=lambda h: (
            tier.get(h.confidence, 9),
            strength.get(h.verdict, 9),
            # Rounded before comparing, so shares that are equal to any meaningful
            # precision are treated as tied and fall through to the name.
            -round(abs(h.explained_share), 6),
            # Final tie-break on the NAME, and it is load-bearing rather than tidy.
            # ltv's three axes tie exactly on (contribution, partial, 100%), so the
            # order fell through to input order, which depends on raw floats that are
            # not bit-reproducible: DuckDB aggregates in parallel and float addition
            # is not associative. Three consecutive runs of the same question ranked
            # them three different ways. That is visible to a user re-asking the same
            # question, not merely to a snapshot test.
            h.dimension,
        ),
    )


def _match_column(row: dict, wanted: str) -> str | None:
    """
    Find *wanted* among a result row's columns, tolerating case and entity prefix.

    Two transformations sit between what a caller asks for and what comes back, and
    both were live bugs:

    * `DuckDBPool.execute()` UPPERCASES every key, to preserve the Snowflake
      DictCursor behaviour everything downstream was written against (CLAUDE.md,
      DuckDB section, point 4).
    * MetricFlow returns the QUALIFIED dimension name. A probe planned from
      `decompose_by` asks for the bare `plan_type`, `format_mf_query()` rewrites it
      to `subscription__plan_type` through the prefix map, and the result column is
      `SUBSCRIPTION__PLAN_TYPE`. Observed live: every dimension of a real churn_rate
      diagnosis failed with "column(s) ['plan_type'] not in result columns
      ['CHURN_RATE', 'SUBSCRIPTION__PLAN_TYPE']", so the answer came back honest but
      empty.

    Matching on the `__` suffix rather than consulting the prefix map keeps this
    module free of any gateway import — and it cannot mismatch, because
    `<entity>__<dim>` puts the dimension last by construction. An exact match always
    wins, so a genuine column called `country` is never shadowed by
    `subscriber__country`.
    """
    lowered = {k.lower(): k for k in row}
    target = wanted.lower()

    if target in lowered:
        return lowered[target]

    # Asked bare, got qualified.
    suffix = f"__{target}"
    candidates = [orig for low, orig in lowered.items() if low.endswith(suffix)]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        # Two columns ending the same way means the caller must be specific; picking
        # one would silently decompose by the wrong entity's version of the column.
        return None

    # Asked qualified, got bare — the reverse, for a caller passing warmup_matrix
    # style names at a result that was never prefixed.
    if "__" in target:
        bare = target.rsplit("__", 1)[-1]
        return lowered.get(bare)

    return None


def rows_to_buckets(
    rows: Sequence[dict],
    dimension_column: str,
    value_column: str,
    weight_column: str | None = None,
) -> list[Bucket]:
    """
    Adapt warehouse rows to Buckets.

    Column names are matched case-insensitively because `DuckDBPool.execute()`
    uppercases every key -- it does that to preserve Snowflake's DictCursor
    behaviour, which everything downstream was written against (CLAUDE.md, DuckDB
    section, point 4). A caller passing `subscriber__country` would otherwise match
    nothing and get an empty decomposition rather than an error.

    Rows whose value is None are skipped; a NULL *dimension* is kept as the None
    bucket, since that is unattributed volume rather than absent data.
    """
    if not rows:
        return []

    dim_key = _match_column(rows[0], dimension_column)
    val_key = _match_column(rows[0], value_column)
    w_key = _match_column(rows[0], weight_column) if weight_column else None

    missing = [
        name for name, key in (
            (dimension_column, dim_key), (value_column, val_key)
        ) if key is None
    ]
    if missing:
        raise KeyError(
            f"column(s) {missing} not in result columns {sorted(rows[0])}"
        )

    buckets: list[Bucket] = []
    for row in rows:
        raw = row.get(val_key)
        if raw is None:
            continue
        weight = row.get(w_key) if w_key else None
        buckets.append(
            Bucket(
                label=row.get(dim_key),
                value=float(raw),
                weight=None if weight is None else float(weight),
            )
        )
    return buckets
