"""
core/diagnostics/playbooks.py — turning the driver graph into concrete probes.

The planner fills slots; it never invents a metric or column name. Everything it can
ask for comes from `driver_graph.yml`, which is machine-checked against the registry
and `dimension_coverage.csv`.

**Phase 2 plans deterministically — no LLM.** That was not the original design, and
the reason for the change is worth recording: a probe plan needs the metric (the
existing `IntentExtractor` already extracts it), the dimensions (the driver graph
orders them), and the windows (`windows.py` builds them). None of that requires a
model. Since the binding constraint on this feature is Google's 15 requests/minute
with no working fallback, a planner that costs zero LLM calls is worth more than one
that reasons freely — and a deterministic plan is reproducible, which an LLM plan is
not.

An LLM planner becomes worth adding when the deterministic order demonstrably picks
the wrong dimensions first. Until then this is both cheaper and more testable.

The probe set for one dimension is a PAIR: the same metric and dimension over the
target window and over the comparison window. `analysis.decompose()` needs both sides,
and asking for one is the most common way to end up describing a level as if it were a
change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Sequence

import yaml

from core.diagnostics.windows import Window

_GRAPH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver_graph.yml")

# What a probe is for. `weight` probes fetch a ratio's denominator so
# `decompose()` can split mix from rate; without them only the additive
# decomposition is available.
ProbeRole = Literal["baseline", "target", "comparison", "weight_target",
                    "weight_comparison", "driver", "quality", "trend"]


@dataclass(frozen=True)
class Probe:
    """One planned query, with enough context to pair it up afterwards."""

    metric: str
    role: ProbeRole
    window: Window
    dimensions: list[str] = field(default_factory=list)
    dimension: str = ""       # the bare dimension this probe decomposes by
    filters: list[Any] = field(default_factory=list)
    label: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity for pairing target with comparison: (metric, dimension, role)."""
        return (self.metric, self.dimension, self.role)


@dataclass(frozen=True)
class ProbePlan:
    """
    A round's worth of probes plus the metadata the answer needs.

    `cautions` travels with the plan rather than being looked up later, so the
    metric-specific traps reach synthesis even if the analysis stage found nothing.
    """

    metric: str
    target: Window
    comparison: Window
    probes: list[Probe]
    dimensions: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    weight_metric: str = ""
    notes: list[str] = field(default_factory=list)
    trend: Window | None = None   # longer trailing window, for baseline sanity


class DriverGraph:
    """Read-only accessor over driver_graph.yml."""

    def __init__(self, data: dict) -> None:
        self._data = data
        self._metrics: dict[str, dict] = data.get("metrics", {}) or {}
        self._defaults: dict = data.get("defaults", {}) or {}

    @classmethod
    def load(cls, path: str | None = None) -> "DriverGraph":
        with open(path or _GRAPH_PATH, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh) or {})

    # ── lookups ───────────────────────────────────────────────────────────────

    def knows(self, metric: str) -> bool:
        return metric in self._metrics

    def entry(self, metric: str) -> dict:
        return self._metrics.get(metric, {}) or {}

    def decompose_by(self, metric: str) -> list[str]:
        return list(self.entry(metric).get("decompose_by") or [])

    def drivers(self, metric: str) -> list[str]:
        return list(self.entry(metric).get("drivers") or [])

    def quality_signals(self, metric: str) -> list[str]:
        return list(self.entry(metric).get("quality_signals") or [])

    def cautions(self, metric: str) -> list[str]:
        return list(self.entry(metric).get("cautions") or [])

    def weight_metric(self, metric: str) -> str:
        return self.entry(metric).get("weight_metric") or ""

    def numerator_metric(self, metric: str) -> str:
        return self.entry(metric).get("numerator_metric") or ""

    @property
    def preferred_order(self) -> list[str]:
        return list(self._defaults.get("preferred_decomposition_order") or [])

    def decompose_first(self, metric: str) -> list[str]:
        """Dimensions this metric wants probed ahead of the global preference."""
        return list(self.entry(metric).get("decompose_first") or [])

    def ordered_dimensions(self, metric: str, limit: int | None = None) -> list[str]:
        """
        Decomposition axes, highest-yield first.

        Three tiers, in order:

        1. `decompose_first` — the metric's OWN priority, which beats the global
           preference. This exists because the global list is a business-wide prior
           and some metrics have a better one. `payment_failure_rate` is the case:
           `failure_reason` separates a card-quality problem (invalid_card,
           card_expired) from a bank or risk one (bank_declined, fraud_detected),
           which is the split that decides who fixes it. A live run picked
           plan_type / country / acquisition_channel and never touched it, because
           the global order put those first.
        2. `preferred_decomposition_order` — the business-wide prior. Sorting
           alphabetically instead would put `acquisition_channel` before `plan_type`
           for no reason.
        3. whatever remains, in file order.

        Anything in `decompose_first` that is not also in `decompose_by` is ignored:
        `decompose_by` is the verified set and stays the single gate on what can be
        asked.
        """
        available = self.decompose_by(metric)
        first = [d for d in self.decompose_first(metric) if d in available]
        preferred = [d for d in self.preferred_order
                     if d in available and d not in first]
        rest = [d for d in available if d not in first and d not in preferred]
        ordered = first + preferred + rest
        return ordered[:limit] if limit else ordered


# ─────────────────────────────────────────────────────────── planning

def _bare(dimension: str) -> str:
    """Strip an entity prefix: `subscriber__country` -> `country`."""
    return dimension.rsplit("__", 1)[-1].lower()


def _pinned_dimensions(filters: Sequence[Any]) -> set[str]:
    """
    Bare names of dimensions a filter narrows to a SINGLE value.

    Only `eq` and a one-element `in` count. A multi-value `in` still leaves
    something to decompose ("MRR for basic and standard"), and an inequality does
    not collapse the axis at all.
    """
    pinned: set[str] = set()
    for clause in filters or []:
        operator_name = str(getattr(clause, "operator", "")).lower()
        value = getattr(clause, "value", None)
        column = getattr(clause, "column", "")
        if not column:
            continue
        if operator_name == "eq" and not isinstance(value, (list, tuple)):
            pinned.add(_bare(str(column)))
        elif operator_name == "in" and isinstance(value, (list, tuple)) and len(value) == 1:
            pinned.add(_bare(str(column)))
    return pinned


def plan_time_comparison(
    metric: str,
    target: Window,
    *,
    graph: DriverGraph,
    comparison: Window | None = None,
    max_dimensions: int = 3,
    include_weights: bool = True,
    filters: Sequence[Any] = (),
    weight_available: Any = None,
    trend_months: int = 12,
) -> ProbePlan:
    """
    Plan a "why did this move" diagnosis: target window versus a comparison window.

    Produces, in order:
      * a baseline pair — the metric with no breakdown, both windows. This is what
        establishes that there IS a gap, and its size. Skipping it means every
        share is computed against a total nobody checked.
      * a target/comparison pair per dimension, up to *max_dimensions*
      * a weight pair per dimension when the metric has a `weight_metric`, so the
        mix-vs-rate split is available rather than only the additive one

    So the probe count is 2 + 2*d without weights, 2 + 4*d with. At three dimensions
    that is 14 probes, ~60 ms each warm — under a second of warehouse time.

    Args:
        comparison: defaults to the equal-length window immediately before *target*,
            month-aligned so it cannot overlap. See `windows.previous_period`.
        filters: carried onto EVERY probe. A diagnosis scoped to one country must
            keep that scope on the comparison side too, or it compares a segment
            against the whole population and attributes the difference to time.
    """
    if not graph.knows(metric):
        raise KeyError(
            f"'{metric}' has no driver_graph entry, so there is no playbook for it"
        )

    comparison = comparison or previous_window(target)

    # A dimension the filters already pin to one value is not a decomposition axis:
    # it yields a single bucket holding 100% of the gap, which reads as a finding and
    # is arithmetic tautology. Observed live on "why is revenue lower in Germany",
    # where `country = DE` was reported as accounting for 100.0% of the gap. Same
    # shape as `expansion_mrr x mrr_type`, whose own metric filter pins mrr_type.
    pinned = _pinned_dimensions(filters)
    dimensions = [
        d for d in graph.ordered_dimensions(metric)
        if _bare(d) not in pinned
    ][:max_dimensions]
    weight = graph.weight_metric(metric) if include_weights else ""
    notes: list[str] = []
    if include_weights and not weight:
        notes.append(
            f"{metric} has no weight_metric, so only the additive decomposition is "
            "available - mix and rate cannot be separated"
        )

    # A longer trailing window ending where the comparison ends. One extra probe
    # (~60ms) that answers "was the baseline typical" -- the question a live June
    # diagnosis could not answer, reporting +163.7% against a May that was itself 41%
    # below trend. See analysis.baseline_representativeness.
    trend = trailing_window(comparison, months=trend_months)

    probes: list[Probe] = [
        Probe(metric=metric, role="baseline", window=target, filters=list(filters),
              label=f"{metric}, target window"),
        Probe(metric=metric, role="comparison", window=comparison, filters=list(filters),
              label=f"{metric}, comparison window"),
        # Grouped BY MONTH, not aggregated over the whole window. A ratio metric
        # aggregated across 12 months is not 12x a monthly rate: churn_rate over
        # 2025-06..2026-05 returned 0.206, because the denominator is a DISTINCT
        # subscriber count over the whole window. Dividing that by 12 gave 1.72%
        # against a true monthly average of 2.61%, and the check then declared an
        # unrepresentative baseline representative. Per-month rows can just be
        # averaged.
        Probe(metric=metric, role="trend", window=trend,
              dimensions=["metric_time__month"], filters=list(filters),
              label=f"{metric}, trend window"),
    ]

    skipped_weights: list[str] = []
    for dim in dimensions:
        probes.append(Probe(metric=metric, role="target", window=target,
                            dimensions=[dim], dimension=dim, filters=list(filters),
                            label=f"{metric} by {dim}, target"))
        probes.append(Probe(metric=metric, role="comparison", window=comparison,
                            dimensions=[dim], dimension=dim, filters=list(filters),
                            label=f"{metric} by {dim}, comparison"))

        # A weight metric does not necessarily certify every dimension the metric it
        # weights does. `churn_rate` reaches `subscriber__country` but its own
        # denominator `monthly_subscriber_base` is an internal metric that never got
        # the second-pass enrichment, so it certifies only sem_mrr's own four. Live
        # run: four weight probes per diagnosis were planned, rejected by the
        # validator, and burned budget for nothing. Skipping them costs only the
        # mix/rate split for that dimension, which `decompose()` already reports.
        if weight and dim and weight_available is not None and not weight_available(weight, dim):
            skipped_weights.append(dim)
            continue

        if weight:
            probes.append(Probe(metric=weight, role="weight_target", window=target,
                                dimensions=[dim], dimension=dim, filters=list(filters),
                                label=f"{weight} by {dim}, target (weight)"))
            probes.append(Probe(metric=weight, role="weight_comparison",
                                window=comparison, dimensions=[dim], dimension=dim,
                                filters=list(filters),
                                label=f"{weight} by {dim}, comparison (weight)"))

    if skipped_weights:
        notes.append(
            f"{weight} does not cover {', '.join(skipped_weights)}, so those are "
            "decomposed additively - mix and rate are not separated for them"
        )

    return ProbePlan(
        metric=metric, target=target, comparison=comparison, probes=probes,
        dimensions=dimensions, cautions=graph.cautions(metric),
        weight_metric=weight, notes=notes, trend=trend,
    )


def trailing_window(anchor: Window, months: int = 12) -> Window:
    """
    The *months* whole months ending where *anchor* ends.

    Used for the baseline sanity check, so it deliberately INCLUDES the comparison
    window: the question is whether that window was typical of recent history, and
    recent history reasonably contains it. It excludes the target, which would
    otherwise pull the trend toward the very movement being explained.
    """
    from core.diagnostics.windows import trailing_months as _trailing

    return _trailing(anchor.end, months, inclusive=True)


def previous_window(target: Window) -> Window:
    """
    Default comparison window. Thin wrapper so callers do not have to remember the
    `monthly_grain` argument, which is the safe default for every metric on
    `fct_mrr_monthly` and harmless elsewhere.
    """
    from core.diagnostics.windows import previous_period

    return previous_period(target, monthly_grain=True)


def pair_findings(
    plan: ProbePlan, findings: Iterable[Any]
) -> dict[str, dict[str, Any]]:
    """
    Group executed findings by dimension and role, ready for `decompose()`.

    Returns ``{dimension: {role: Finding}}`` with the baseline pair under the empty
    string. Findings that errored are dropped here, so a dimension missing one half
    of its pair simply has no entry rather than being silently decomposed against
    nothing.
    """
    by_label = {}
    for finding in findings:
        by_label[finding.label] = finding

    grouped: dict[str, dict[str, Any]] = {}
    for probe in plan.probes:
        finding = by_label.get(probe.label)
        if finding is None or not finding.ok:
            continue
        grouped.setdefault(probe.dimension, {})[probe.role] = finding
    return grouped


def usable_pairs(grouped: dict[str, dict[str, Any]]) -> list[str]:
    """
    Dimensions with both sides present — the only ones that can be decomposed.

    A dimension whose comparison probe failed cannot yield a gap, and treating a
    missing side as zero would report the whole level as the change.
    """
    return [
        dim for dim, roles in grouped.items()
        if dim and "target" in roles and "comparison" in roles
    ]
