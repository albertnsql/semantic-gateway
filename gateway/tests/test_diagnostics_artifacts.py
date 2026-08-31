"""
tests/test_diagnostics_artifacts.py — warnings that fire when, and only when, they apply.

Both halves of this were demonstrated by one live answer on 2026-08-28. A user asked
why churn was high in June 2026 and got:

* a warning about the trailing churn-only month — while the target window was JUNE
  and the newest period was AUGUST. It was the most alarming line in the answer and
  had nothing to do with it.
* no mention at all of June 2026 being the month of a documented bad append, which is
  the one fact that should have changed how the answer was read.

A warning that fires when it does not apply teaches readers to ignore warnings, so
over-firing is not the safe direction here.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.diagnostics.artifacts import Artifact, ArtifactRegistry
from core.diagnostics.windows import Window

JUNE = Window.of("2026-06-01", "2026-06-30")
MAY = Window.of("2026-05-01", "2026-05-31")
H1_2026 = Window.of("2026-01-01", "2026-06-30")
H2_2025 = Window.of("2025-07-01", "2025-12-31")
AUGUST = Window.of("2026-08-01", "2026-08-31")


@pytest.fixture(scope="module")
def registry() -> ArtifactRegistry:
    return ArtifactRegistry.load()


class TestShippedFile:
    def test_it_loads_and_every_entry_is_usable(self, registry: ArtifactRegistry) -> None:
        artifacts = registry.all()
        assert artifacts
        for a in artifacts:
            assert a.id and a.summary, f"{a.id or '(unnamed)'} is missing id or summary"
            assert a.severity in ("high", "medium", "low")

    def test_ids_are_unique(self, registry: ArtifactRegistry) -> None:
        ids = [a.id for a in registry.all()]
        assert len(ids) == len(set(ids))

    def test_summaries_are_single_line(self, registry: ArtifactRegistry) -> None:
        """YAML folded blocks keep newlines; those break the answer's line structure."""
        for a in registry.all():
            assert "\n" not in a.summary and "\n" not in a.guidance


class TestRetiredArtifacts:
    """
    An artifact whose problem has been REPAIRED must stop firing while staying on
    record. Warning about a fixed problem is the same failure as warning about the
    wrong window: it tells a reader a real finding might be fake.

    June 2026 is the case. Verified against the warehouse on 2026-08-28: zero orphan
    subscribers in fct_mrr_monthly (12,532 rows), fct_payments (8,983) or
    fct_stream_sessions (116,493) — that session count matching the documented true
    height, so the 4x inflation is gone — and zero null-country payments in any month.
    """

    def test_the_june_append_is_recorded_but_retired(
        self, registry: ArtifactRegistry
    ) -> None:
        june_entry = [a for a in registry.all() if a.id == "june-2026-orphan-append"]
        assert june_entry, (
            "the entry must survive as institutional memory - a rebuild from "
            "unrepaired CSVs brings the problem back and there would be nothing left "
            "to describe it"
        )
        assert june_entry[0].verified_clean == "2026-08-28"
        assert june_entry[0] not in registry.active()

    def test_a_june_diagnosis_now_carries_no_warning(
        self, registry: ArtifactRegistry
    ) -> None:
        """The live question. Churn in June is genuinely up; the data is clean."""
        assert registry.applicable("churn_rate", JUNE, MAY,
                                   today=date(2026, 8, 28)) == []
        assert registry.applicable("total_revenue", JUNE,
                                   today=date(2026, 8, 28)) == []

    def test_retirement_does_not_silence_everything(
        self, registry: ArtifactRegistry
    ) -> None:
        """Two of four are retired; the live ones must still fire."""
        assert len(registry.active()) == 2
        assert {a.id for a in registry.active()} == {
            "mrr-spine-trailing-month", "content-id-joins-broken"
        }

    def test_a_retired_artifact_never_matches_regardless_of_window(self) -> None:
        reg = ArtifactRegistry([
            Artifact(id="fixed", summary="s", verified_clean="2026-08-28"),
        ])
        assert reg.applicable("anything", JUNE) == []
        assert reg.all(), "retired is not deleted"


class TestTrailingMonthIsRelative:
    """The half that fired when it should not have."""

    def test_it_fires_when_the_window_reaches_the_current_month(
        self, registry: ArtifactRegistry
    ) -> None:
        today = date(2026, 8, 28)
        hits = registry.applicable("churn_rate", AUGUST, Window.of("2026-07-01", "2026-07-31"),
                                   today=today)
        assert any(a.id == "mrr-spine-trailing-month" for a in hits)

    def test_it_stays_quiet_for_the_june_diagnosis_that_exposed_it(
        self, registry: ArtifactRegistry
    ) -> None:
        """
        The live bug, pinned. On 2026-08-28 a June-vs-May diagnosis carried the
        trailing-month warning. It must not any more.
        """
        hits = registry.applicable("churn_rate", JUNE, MAY, today=date(2026, 8, 28))
        assert not any(a.id == "mrr-spine-trailing-month" for a in hits)

    def test_it_moves_with_the_calendar(self, registry: ArtifactRegistry) -> None:
        """
        A fixed window would be wrong the following month and silently stop firing,
        which is why this one is relative.
        """
        june_today = date(2026, 6, 15)
        assert any(a.id == "mrr-spine-trailing-month"
                   for a in registry.applicable("churn_rate", JUNE, today=june_today))

    def test_it_covers_periods_BEYOND_the_current_month(
        self, registry: ArtifactRegistry
    ) -> None:
        """
        The spine runs AHEAD of the calendar. Checked against the real warehouse on
        2026-08-28: max(period_month) was 2026-09-01, holding 531 rows of which all
        531 were churned — a 100% rate. Scoped to `current_month` alone this warning
        resolved to August and stayed silent for the exact period it describes.
        """
        september = Window.of("2026-09-01", "2026-09-30")
        hits = registry.applicable("churn_rate", september, today=date(2026, 8, 28))
        assert any(a.id == "mrr-spine-trailing-month" for a in hits), (
            "the phantom period sits one month ahead of the calendar and must "
            "still trigger the warning"
        )

    def test_it_does_not_apply_to_a_payments_metric(
        self, registry: ArtifactRegistry
    ) -> None:
        """It is an fct_mrr_monthly spine problem, not a warehouse-wide one."""
        hits = registry.applicable("total_revenue", AUGUST, today=date(2026, 8, 28))
        assert not any(a.id == "mrr-spine-trailing-month" for a in hits)


class TestMatching:
    def test_severity_orders_the_output(self) -> None:
        """
        A `high` artifact means the finding is probably not real. Burying it under a
        `medium` note inverts what the reader needs first.
        """
        reg = ArtifactRegistry([
            Artifact(id="b-medium", summary="m", severity="medium"),
            Artifact(id="a-high", summary="h", severity="high"),
        ])
        assert [a.id for a in reg.applicable("mrr", JUNE)] == ["a-high", "b-medium"]

    def test_an_artifact_with_no_window_always_applies(self) -> None:
        reg = ArtifactRegistry([Artifact(id="always", summary="s")])
        assert reg.applicable("mrr", H1_2026)

    def test_metric_scoping_is_respected(self) -> None:
        reg = ArtifactRegistry([
            Artifact(id="only-churn", summary="s", metrics=("churn_rate",)),
        ])
        assert reg.applicable("churn_rate", JUNE)
        assert not reg.applicable("total_revenue", JUNE)

    def test_a_none_window_is_skipped_not_treated_as_matching(self) -> None:
        """A diagnosis with no comparison must not match every windowed artifact."""
        reg = ArtifactRegistry([
            Artifact(id="june", summary="s", window=JUNE),
        ])
        assert not reg.applicable("mrr", H2_2025, None)

    def test_metrics_all_is_not_read_as_a_metric_named_all(self, tmp_path) -> None:
        """
        `metrics: all` arrives from YAML as a scalar. Wrapping it in a list would match
        a metric literally called "all" and nothing else — silently disabling the
        broadest artifacts.
        """
        path = tmp_path / "a.yml"
        path.write_text(
            "version: 1\nartifacts:\n  - id: x\n    summary: s\n    metrics: all\n",
            encoding="utf-8",
        )
        reg = ArtifactRegistry.load(str(path))
        assert reg.all()[0].metrics == ()
        assert reg.applicable("anything_at_all", JUNE)


class TestPurity:
    def test_no_heavy_imports(self) -> None:
        """Same rule as analysis.py and windows.py — cheap to test, no gateway deps."""
        import subprocess
        import sys

        code = (
            "import sys, core.diagnostics.artifacts;"
            "print(','.join(m for m in ('config','duckdb','openai','metricflow','dbt') "
            "if m in sys.modules))"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-400:]
        assert not proc.stdout.strip(), f"artifacts.py pulls in: {proc.stdout.strip()}"
