"""
core/diagnostics/artifacts.py — window-scoped warnings about the data itself.

A diagnostic agent finds whatever moved and cannot tell a real churn spike from a bad
data append, because they are identical in the numbers. This module is what stops the
second being narrated as the first.

Two rules, both learned from one live answer on 2026-08-28:

* **Only surface what applies.** That answer warned "the newest period is churn-only
  ... reads as a catastrophic churn spike" while diagnosing JUNE, with August being
  the newest period. A warning that fires when it does not apply teaches readers to
  ignore warnings, which costs more than the warning was worth.
* **Cover the window actually used.** The same answer said nothing about June 2026
  being the month of a documented bad append — the one fact that should have changed
  how it was read.

Stdlib plus yaml only, no gateway imports, so it stays as cheap to test as
`analysis.py` and `windows.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Sequence

import yaml

from core.diagnostics.windows import Window, month_end, month_start

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_artifacts.yml")

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class Artifact:
    """One known data problem, scoped to a window and a set of metrics."""

    id: str
    summary: str
    guidance: str = ""
    severity: str = "medium"
    window: Window | None = None      # None = applies regardless of window
    relative: str = ""                # e.g. "current_month", resolved at match time
    metrics: tuple[str, ...] = ()     # empty = all metrics
    # Set once the warehouse has been checked and the problem is gone. The entry
    # STAYS — deleting it loses the institutional memory, and a rebuild from
    # unrepaired CSVs would bring the problem back with nothing left to describe it —
    # but it stops firing. A warning about a repaired problem is the same failure as
    # a warning scoped to the wrong window: it tells a reader a real finding might be
    # fake, and teaches them to discount warnings.
    verified_clean: str = ""          # ISO date of the verification

    def covers_metric(self, metric: str) -> bool:
        return not self.metrics or metric in self.metrics

    def resolve_window(self, today: date | None = None) -> Window | None:
        """
        The window this artifact occupies, resolving a relative spec against *today*.

        Relative forms exist because fct_mrr_monthly's trailing churn-only period
        moves with the calendar — a fixed window would be wrong the following month
        and silently stop firing.

        `current_month_onward` rather than `current_month`, and the difference is not
        cosmetic. The spine runs AHEAD of the calendar: on 2026-08-28 the newest
        period in the fact table was 2026-09-01, holding 531 rows of which 531 were
        churned — a 100% churn rate. Scoped to the current month alone, the warning
        would have resolved to August and stayed silent for the very period it exists
        to describe.
        """
        anchor = today or date.today()
        if self.relative == "current_month_onward":
            return Window(month_start(anchor), date(9999, 12, 31))
        if self.relative == "current_month":
            return Window(month_start(anchor), month_end(anchor))
        return self.window

    def applies_to(
        self, metric: str, windows: Sequence[Window], today: date | None = None
    ) -> bool:
        """
        Whether this artifact touches a diagnosis of *metric* over *windows*.

        Both the target AND the comparison window are checked: an artifact in the
        baseline distorts the gap exactly as much as one in the target, and only
        checking the target would miss half the cases.
        """
        if self.verified_clean:
            return False
        if not self.covers_metric(metric):
            return False
        scope = self.resolve_window(today)
        if scope is None:
            return True
        return any(scope.overlaps(w) for w in windows if w is not None)


class ArtifactRegistry:
    """Read-only accessor over known_artifacts.yml."""

    def __init__(self, artifacts: Sequence[Artifact]) -> None:
        self._artifacts = list(artifacts)

    @classmethod
    def load(cls, path: str | None = None) -> "ArtifactRegistry":
        with open(path or _PATH, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        artifacts: list[Artifact] = []
        for entry in data.get("artifacts") or []:
            raw_window = entry.get("window") or {}
            window = None
            if raw_window.get("start") and raw_window.get("end"):
                window = Window.of(raw_window["start"], raw_window["end"])
            metrics = entry.get("metrics") or []
            # `metrics: all` is spelled as the string "all", which YAML gives us as a
            # scalar rather than a list. Treating it as a one-element list would match
            # a metric literally named "all" and nothing else.
            if isinstance(metrics, str):
                metrics = [] if metrics.lower() == "all" else [metrics]
            artifacts.append(
                Artifact(
                    id=entry.get("id", ""),
                    summary=" ".join((entry.get("summary") or "").split()),
                    guidance=" ".join((entry.get("guidance") or "").split()),
                    severity=entry.get("severity", "medium"),
                    window=window,
                    relative=entry.get("relative", ""),
                    metrics=tuple(metrics),
                    verified_clean=str(entry.get("verified_clean") or ""),
                )
            )
        return cls(artifacts)

    def all(self) -> list[Artifact]:
        """Every entry, including retired ones. For tests and documentation."""
        return list(self._artifacts)

    def active(self) -> list[Artifact]:
        """Entries that can still fire - everything not marked verified_clean."""
        return [a for a in self._artifacts if not a.verified_clean]

    def applicable(
        self,
        metric: str,
        *windows: Window | None,
        today: date | None = None,
    ) -> list[Artifact]:
        """
        Artifacts touching this diagnosis, most severe first.

        Returning them ranked matters: a `high` artifact means the finding is probably
        not real, and burying that under a `medium` note about an unattributed bucket
        inverts what the reader needs to see.
        """
        present = [w for w in windows if w is not None]
        hits = [a for a in self._artifacts if a.applies_to(metric, present, today)]
        return sorted(hits, key=lambda a: (_SEVERITY_ORDER.get(a.severity, 9), a.id))
