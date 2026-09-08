"""
core/diagnostics/state.py — the data contract for a diagnosis.

Deliberately imports NOTHING from the gateway. `analysis.py` operates only on the
types defined here, which is what lets the contribution math be tested with no
warehouse, no manifest, no LLM and no network — and that math is where the whole
feature's value sits, so it has to be cheap to test exhaustively.

The one consequence to know: `Finding.intent` is a plain dict rather than a
`QueryIntent`. `tools.py` does the conversion. That also keeps a Finding trivially
serialisable, which the response envelope and the diagnosis cache both need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Sequence, TypedDict

# Where a bucket exists. A segment present on only one side of a comparison is a
# finding in itself -- a plan launched mid-period, a country that stopped selling --
# and silently dropping it is how a decomposition ends up not summing to the gap.
Presence = Literal["both", "target_only", "comparison_only"]

# How much a claim is worth. `contribution` is arithmetic: this dimension accounts
# for N% of the gap, and it is checkable. `association` is not: the numbers move
# together and nothing here establishes why. They must never be rendered alike.
Confidence = Literal["contribution", "association"]

Verdict = Literal["explains", "partial", "not_it", "inconclusive"]


# ─────────────────────────────────────────────────────────── evidence

@dataclass(frozen=True)
class Finding:
    """
    One executed probe: what was asked, what came back, and how it was produced.

    `id` is what a synthesised sentence cites ("[F3]"). Carrying `sql` alongside the
    rows is the point -- a causal claim that cannot name the query behind it does not
    survive the citation check in synthesis.
    """

    id: str
    label: str
    metric: str
    dimensions: list[str]
    rows: list[dict[str, Any]]
    sql: str = ""
    intent: dict[str, Any] = field(default_factory=dict)
    from_cache: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def row_count(self) -> int:
        return len(self.rows)


# ─────────────────────────────────────────────────────────── decomposition

@dataclass(frozen=True)
class Bucket:
    """
    One dimension value and its measured amount.

    `label` of None is the NULL bucket and is kept on purpose. Dropping it hides
    unattributed volume, which is exactly how 24.6% of 2026 revenue once sat in an
    "Unknown" country bucket without anyone noticing.

    `weight` is the denominator behind `value` -- subscriber count, session count --
    and is only needed for a rate or average, where a total moves either because the
    mix of buckets changed or because the rate within them did. A sum metric needs
    no weight; see `decompose()`.
    """

    label: str | None
    value: float
    weight: float | None = None


@dataclass(frozen=True)
class Contribution:
    """How much of the gap one bucket accounts for."""

    label: str | None
    target: float
    comparison: float
    delta: float
    share: float                    # signed fraction of the total gap
    presence: Presence = "both"
    mix_effect: float | None = None   # composition changed
    rate_effect: float | None = None  # within-bucket rate changed

    @property
    def is_null_bucket(self) -> bool:
        return self.label is None


@dataclass(frozen=True)
class Decomposition:
    """
    The result of comparing one dimension's buckets across two sides.

    `residual` is the part the buckets fail to account for. It should be ~0 by
    construction; a non-trivial value means the inputs were inconsistent (a filtered
    probe compared against an unfiltered baseline, say) and the decomposition should
    not be reported. `exact` is that check.
    """

    dimension: str
    target_total: float
    comparison_total: float
    gap: float
    contributions: list[Contribution]
    residual: float = 0.0
    weighted: bool = False
    note: str = ""

    @property
    def exact(self) -> bool:
        scale = max(abs(self.target_total), abs(self.comparison_total), 1e-9)
        return abs(self.residual) <= 1e-6 * scale

    @property
    def top(self) -> Contribution | None:
        if not self.contributions:
            return None
        return self.contributions[0]


# ─────────────────────────────────────────────────────────── hypotheses

@dataclass(frozen=True)
class Hypothesis:
    """
    A candidate explanation, with its evidence and its honest strength.

    `confidence` is the tier, `verdict` is the strength within it. A `not_it` is a
    genuinely useful result -- "payment failure rate matches peers, so it is not
    that" is often the most valuable line in an answer and costs nothing extra.
    """

    dimension: str
    statement: str
    confidence: Confidence
    verdict: Verdict
    explained_share: float
    evidence: list[str] = field(default_factory=list)
    decomposition: Decomposition | None = None

    @property
    def reportable(self) -> bool:
        """Whether this belongs in the answer at all (a ruled-out one does)."""
        return self.verdict in ("explains", "partial", "not_it")


# ─────────────────────────────────────────────────────────── budget

@dataclass
class Budget:
    """
    Hard caps on a diagnosis.

    The characteristic failure of a diagnostic agent is not stopping -- it keeps
    planning probes and eventually narrates noise. These bounds are checked on the
    graph's conditional edge, and `spent` is the reason a run can end with "I checked
    four things and none of them explains it", which is a legitimate answer.
    """

    max_rounds: int = 3
    # Matches config.diagnostics_max_probes, which the route always supplies. This
    # default only applies where no budget is passed (test harnesses,
    # scratch/try_diagnosis.py) — and at 12 it starved the reflect loop there while
    # production ran fine, which is the most confusing way for the two to disagree.
    # 40 is the measured worst case: engagement_rate reaches 9 axes over 3 rounds
    # and is weighted, so 3 + 9*4 = 39.
    max_probes: int = 40
    deadline_seconds: float = 25.0
    probes_used: int = 0
    rounds_used: int = 0
    seconds_used: float = 0.0

    @property
    def spent(self) -> bool:
        return (
            self.rounds_used >= self.max_rounds
            or self.probes_used >= self.max_probes
            or self.seconds_used >= self.deadline_seconds
        )

    @property
    def probes_remaining(self) -> int:
        return max(0, self.max_probes - self.probes_used)

    def why_spent(self) -> str:
        """Human-readable reason, for the answer when a diagnosis stops early."""
        if self.rounds_used >= self.max_rounds:
            return f"reached the {self.max_rounds}-round limit"
        if self.probes_used >= self.max_probes:
            return f"reached the {self.max_probes}-probe limit"
        if self.seconds_used >= self.deadline_seconds:
            return f"reached the {self.deadline_seconds:.0f}s deadline"
        return ""


# ─────────────────────────────────────────────────────────── graph state

class DiagnosticState(TypedDict, total=False):
    """
    The state threaded through the graph.

    A TypedDict rather than a pydantic model on purpose: LangGraph validates and
    merges this on every superstep, and a BaseModel pays validation each time for no
    benefit here. The values inside it are typed dataclasses, so the structure is
    still checked where it matters.

    `findings` needs an additive reducer once this reaches LangGraph
    (`Annotated[list[Finding], operator.add]`), because the probe stage writes to it
    from several concurrent nodes and the default rejects that.
    """

    question: str
    request_id: str
    base_metric: str
    baseline: Finding | None
    findings: list[Finding]
    hypotheses: list[Hypothesis]
    budget: Budget
    answer: str
    stopped_because: str


def next_finding_id(existing: Sequence[Finding]) -> str:
    """
    Allocate the next citation id. Sequential and stable within one diagnosis.

    Not a uuid: these appear in prose the reader is meant to follow ("[F3]"), so they
    have to be short and ordered.
    """
    return f"F{len(existing) + 1}"
