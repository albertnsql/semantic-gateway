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

    if top is None or top_share < min_share:
        return Hypothesis(
            dimension=dim,
            statement=(
                f"{dim} does not explain the gap - no single value accounts for "
                f"more than {top_share:.1%} of it."
            ),
            confidence=confidence,
            verdict="not_it",
            explained_share=share,
            evidence=list(evidence),
            decomposition=decomposition,
        )

    label = "(not set)" if top.label is None else top.label
    direction = "lower" if decomposition.gap < 0 else "higher"
    verdict: Verdict = "explains" if top_share >= strong_share else "partial"

    parts = [
        f"{dim} = {label} accounts for {top_share:.1%} of the gap "
        f"({_fmt(top.delta)} of {_fmt(decomposition.gap)})"
    ]
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

    return all(
        concentration(h.decomposition)[0] < dominant_share for h in graded
    )


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
            -abs(h.explained_share),
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
