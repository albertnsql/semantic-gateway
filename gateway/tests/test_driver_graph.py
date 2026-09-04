"""
tests/test_driver_graph.py — the driver graph may only reference verified reality.

`core/diagnostics/driver_graph.yml` is what the diagnostic planner instantiates
probes from, so a stale entry does not fail loudly: the planner emits a probe, the
route rejects it or MetricFlow refuses it, and the agent reaches its conclusion
with a hole in the evidence it does not know about.

Two documents have to stay in step, and both are checked here:

* the **registry** — every metric name mentioned must exist and be user-facing
* **dimension_coverage.csv** — every `decompose_by` pair must be marked ok, i.e.
  verified to compile by `python audit_dimension_coverage.py`

This is the same failure mode `run_evals.py --check-drift` was built for after
three time columns rotted unnoticed in the eval fixture. A hand-curated file that
names columns will drift; the only question is whether anything notices.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest
import yaml

from core.manifest_parser import ManifestParser
from core.metric_registry import MetricRegistry

_HERE = os.path.dirname(os.path.abspath(__file__))
_GATEWAY = os.path.dirname(_HERE)
_GRAPH_PATH = os.path.join(_GATEWAY, "core", "diagnostics", "driver_graph.yml")
_COVERAGE_PATH = os.path.join(_GATEWAY, "dimension_coverage.csv")

# The `registry` fixture in test_metric_registry.py is module-local, not in
# conftest, so it is rebuilt here against the same real YAML rather than moving a
# fixture other tests already depend on.
_DBT_ROOT = Path(_GATEWAY).parent / "dbt_streaming_analytics" / "streaming_analytics"

# Verified-but-useless for diagnosis, and excluded from every decompose_by on
# purpose (see the header of driver_graph.yml). Grouping a metric by a raw event
# date is not a finding, and time is handled by time_range instead. cohort_month
# is deliberately NOT in this set — cohort analysis is a real diagnostic lens.
_RAW_DATE_COLUMNS = frozenset({
    "signup_date", "churn_date", "payment_date",
    "session_start", "event_timestamp", "period_month",
})


@pytest.fixture(scope="module")
def graph() -> dict:
    with open(_GRAPH_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _coverage_rows() -> list[dict]:
    if not os.path.exists(_COVERAGE_PATH):
        pytest.skip(
            "dimension_coverage.csv is missing — run `python audit_dimension_coverage.py`. "
            "Skipping rather than passing: an absent baseline cannot verify anything."
        )
    with open(_COVERAGE_PATH, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def verified() -> dict[str, set[str]]:
    """{metric: {bare dimensions that compiled}} from the committed audit."""
    ok: dict[str, set[str]] = {}
    for row in _coverage_rows():
        if row["ok"] == "true":
            ok.setdefault(row["metric"], set()).add(row["bare_dimension"])
    return ok


@pytest.fixture(scope="module")
def unusable() -> dict[tuple[str, str], str]:
    """
    {(metric, bare): verdict} for pairs that compile but cannot decompose anything.

    ``all_null`` means the dimension is empty — the genre case. ``single_value``
    means it resolves to one bucket, which is not a decomposition either: it is how
    ``expansion_mrr x mrr_type`` looks, because that metric's own filter pins
    mrr_type to 'expansion'.

    A blank ``population`` means the audit ran with ``--no-execute`` and the pair is
    simply unknown, not bad, so it is left out — the same treatment the audit's own
    baseline diff gives it.
    """
    bad = {"all_null", "single_value", "exec_error"}
    return {
        (row["metric"], row["bare_dimension"]): row["population"]
        for row in _coverage_rows()
        if row.get("population", "") in bad
    }


@pytest.fixture(scope="module")
def _registry() -> MetricRegistry:
    manifest = _DBT_ROOT / "target" / "manifest.json"
    parser = ManifestParser()
    if manifest.exists():
        parser.load(str(manifest))
    else:
        parser._nodes = {}
        parser._sources = {}
        parser._loaded = True
    reg = MetricRegistry()
    reg.load(str(_DBT_ROOT / "metrics"), str(_DBT_ROOT / "models" / "semantic"), parser)
    return reg


@pytest.fixture(scope="module")
def user_facing(_registry: MetricRegistry) -> set[str]:
    """Metric names the route and the LLM are allowed to see."""
    return {m.name for m in _registry.list_user_facing_metrics()}


@pytest.fixture(scope="module")
def registry_all(_registry: MetricRegistry) -> set[str]:
    """
    Every certified metric, INCLUDING the internal ratio building blocks.

    `weight_metric` legitimately points at one of those: `monthly_subscriber_base` is
    in `_INTERNAL_METRICS` so the LLM never sees it, but it is certified, so the
    validator accepts a planner probe for it.
    """
    return {m.name for m in _registry.list_metrics()}


class TestDriverGraphStructure:
    def test_it_parses_and_declares_a_version(self, graph: dict) -> None:
        assert graph.get("version") == 1
        assert isinstance(graph.get("metrics"), dict) and graph["metrics"]

    def test_every_metric_has_the_expected_fields(self, graph: dict) -> None:
        required = {"decompose_by", "drivers", "identities", "quality_signals", "cautions"}
        for name, entry in graph["metrics"].items():
            missing = required - set(entry)
            assert not missing, f"{name} is missing {sorted(missing)}"

    def test_no_metric_lists_itself_as_its_own_driver(self, graph: dict) -> None:
        """A self-edge would make the planner probe the thing it is explaining."""
        for name, entry in graph["metrics"].items():
            assert name not in (entry.get("drivers") or [])
            assert name not in (entry.get("quality_signals") or [])


class TestDriverGraphNamesAreReal:
    def test_every_key_is_a_user_facing_metric(
        self, graph: dict, user_facing: set[str]
    ) -> None:
        unknown = sorted(set(graph["metrics"]) - user_facing)
        assert not unknown, f"not user-facing metrics: {unknown}"

    def test_every_driver_and_quality_signal_is_a_certified_metric(
        self, graph: dict, registry_all: set[str]
    ) -> None:
        """
        CERTIFIED, not user-facing. The planner reads this file rather than the
        user-facing metric list, so an internal-but-certified metric is a legitimate
        driver — the same reasoning that already licenses `weight_metric` pointing at
        `monthly_subscriber_base`.

        `total_payments` is the case that forced this distinction: it is the
        denominator of payment_failure_rate and hidden from the LLM (nobody asks "how
        many payment attempts were there"), but probing it is exactly how you tell a
        rising failure RATE from falling attempt VOLUME.

        The guard that matters is unchanged: the name must exist in the registry, or
        the probe is rejected mid-run and the diagnosis loses a slot.
        """
        offences: list[str] = []
        for name, entry in graph["metrics"].items():
            for field in ("drivers", "quality_signals"):
                for referenced in entry.get(field) or []:
                    if referenced not in registry_all:
                        offences.append(f"{name}.{field} -> {referenced}")
        assert not offences, "metrics not in the registry: " + "; ".join(offences)

    def test_a_quality_signal_is_never_internal(
        self, graph: dict, user_facing: set[str]
    ) -> None:
        """
        Drivers may be internal because they are probed for arithmetic. A quality
        SIGNAL is different: it is reported to a human as an association, so naming
        one the user can never look up is a dead end in the answer.
        """
        for name, entry in graph["metrics"].items():
            for referenced in entry.get("quality_signals") or []:
                assert referenced in user_facing, (
                    f"{name}.quality_signals -> {referenced} is internal; a signal "
                    "shown to a reader must be a metric they can query"
                )

    def test_every_user_facing_metric_has_an_entry(
        self, graph: dict, user_facing: set[str]
    ) -> None:
        """
        A metric with no entry cannot be diagnosed at all — the planner has no
        playbook to instantiate. Better to fail here than to discover it as an
        unanswerable question in production.
        """
        missing = sorted(user_facing - set(graph["metrics"]))
        assert not missing, f"metrics with no driver-graph entry: {missing}"


class TestDecomposeByIsVerified:
    def test_every_pair_compiled_in_the_audit(
        self, graph: dict, verified: dict[str, set[str]]
    ) -> None:
        """
        The load-bearing check. Every decompose_by entry must appear as ok=true in
        dimension_coverage.csv for that metric.
        """
        offences: list[str] = []
        for name, entry in graph["metrics"].items():
            ok_dims = verified.get(name)
            if ok_dims is None:
                offences.append(f"{name}: absent from the coverage audit entirely")
                continue
            for dim in entry.get("decompose_by") or []:
                if dim not in ok_dims:
                    offences.append(f"{name} x {dim}")
        assert not offences, (
            "decompose_by entries that are not verified to compile: "
            + "; ".join(offences)
            + " — re-run `python audit_dimension_coverage.py` and reconcile."
        )

    def test_no_pair_is_empty_or_single_bucket(
        self, graph: dict, unusable: dict[tuple[str, str], str]
    ) -> None:
        """
        The check that would have stopped the genre incident.

        Compiling is not enough. `content_primary_genre` compiled for all six
        session metrics and returned a single row with genre = null, which the
        narrative reported as "a uniform engagement level across the platform's
        content library". A planner probing it would reach its conclusion with an
        empty slot it does not know about.

        `single_value` is rejected for the same reason at lower severity: one bucket
        is not a decomposition. It is how a dimension pinned by the metric's own
        filter looks.
        """
        offences = [
            f"{name} x {dim} ({unusable[(name, dim)]})"
            for name, entry in graph["metrics"].items()
            for dim in entry.get("decompose_by") or []
            if (name, dim) in unusable
        ]
        assert not offences, (
            "decompose_by entries that compile but cannot decompose: "
            + "; ".join(offences)
            + " — see the population column in dimension_coverage.csv."
        )

    def test_raw_event_dates_stay_excluded(self, graph: dict) -> None:
        """
        These compile, so the audit will not catch them — but grouping a metric by
        a raw event date is not a diagnostic finding, and letting one back in would
        hand the planner a probe that always returns noise.
        """
        offences = [
            f"{name} x {dim}"
            for name, entry in graph["metrics"].items()
            for dim in entry.get("decompose_by") or []
            if dim in _RAW_DATE_COLUMNS
        ]
        assert not offences, "raw event-date columns in decompose_by: " + "; ".join(offences)

    def test_no_duplicate_dimensions_within_a_metric(self, graph: dict) -> None:
        for name, entry in graph["metrics"].items():
            dims = entry.get("decompose_by") or []
            assert len(dims) == len(set(dims)), f"{name} repeats a dimension"

    def test_every_metric_can_be_decomposed_somehow(self, graph: dict) -> None:
        """An empty decompose_by makes the metric undiagnosable."""
        for name, entry in graph["metrics"].items():
            assert entry.get("decompose_by"), f"{name} has no decomposition axis"


class TestWeightMetricsMatchTheSemanticLayer:
    """
    A ratio's `weight_metric` must be its ACTUAL denominator, resolved from the dbt
    YAML rather than trusted.

    This is the one field where being plausibly wrong is worse than being absent.
    `analysis.decompose()` only splits mix from rate when weights are present, and a
    wrong denominator produces a decomposition that does not reconcile — non-zero
    residual, `inconclusive` verdict, and a diagnosis that says "I cannot tell" about
    something it could have explained.

    The file shipped with `churn_rate` naming `total_subscribers` among its drivers
    and no weight at all, while the metric is
    `monthly_churned_subscribers / monthly_subscriber_base`. Same shape as the
    `_METRIC_TIME_COL` bug where two sources disagreed about a column and nothing
    checked; the fix there was also to resolve through the YAML.
    """

    @staticmethod
    def _declared_ratios() -> dict[str, str]:
        """{metric: denominator} for every MetricFlow `ratio` metric."""
        import glob

        out: dict[str, str] = {}
        for path in glob.glob(str(_DBT_ROOT / "metrics" / "*.yml")):
            with open(path, encoding="utf-8") as fh:
                for metric in (yaml.safe_load(fh) or {}).get("metrics", []):
                    params = metric.get("type_params") or {}
                    if metric.get("type") == "ratio" and params.get("denominator"):
                        out[metric["name"]] = params["denominator"]
        return out

    def test_every_ratio_metric_declares_its_real_denominator(self, graph: dict) -> None:
        ratios = self._declared_ratios()
        assert ratios, "no ratio metrics found — the YAML scan is broken, not the graph"

        offences: list[str] = []
        for metric, denominator in ratios.items():
            entry = graph["metrics"].get(metric)
            if entry is None:
                continue  # covered by test_every_user_facing_metric_has_an_entry
            declared = entry.get("weight_metric")
            if declared != denominator:
                offences.append(
                    f"{metric}: weight_metric={declared!r} but the semantic layer "
                    f"says the denominator is {denominator!r}"
                )
        assert not offences, "; ".join(offences)

    def test_every_ratio_metric_declares_its_real_numerator(self, graph: dict) -> None:
        import glob

        numerators: dict[str, str] = {}
        for path in glob.glob(str(_DBT_ROOT / "metrics" / "*.yml")):
            with open(path, encoding="utf-8") as fh:
                for metric in (yaml.safe_load(fh) or {}).get("metrics", []):
                    params = metric.get("type_params") or {}
                    if metric.get("type") == "ratio" and params.get("numerator"):
                        numerators[metric["name"]] = params["numerator"]

        offences = [
            f"{m}: numerator_metric={graph['metrics'][m].get('numerator_metric')!r} != {n!r}"
            for m, n in numerators.items()
            if m in graph["metrics"]
            and graph["metrics"][m].get("numerator_metric") != n
        ]
        assert not offences, "; ".join(offences)

    def test_average_measures_carry_a_count_weight(self, graph: dict) -> None:
        """
        MetricFlow types these `simple`, so the ratio check above cannot see them —
        but avg_completion_pct and friends are means over sessions, and weighting a
        mean by nothing makes the mix/rate split unavailable.
        """
        for metric in ("engagement_rate", "avg_watch_time", "avg_buffering_events"):
            assert graph["metrics"][metric].get("weight_metric") == "total_sessions", (
                f"{metric} is an average over sessions and needs total_sessions as "
                "its weight, or it can only be decomposed additively"
            )

    def test_weights_and_numerators_are_real_certified_metrics(
        self, graph: dict, registry_all: set[str]
    ) -> None:
        """
        These may be INTERNAL (monthly_subscriber_base is hidden from the LLM), but
        they must be certified or the validator will reject the weight probe at
        runtime — after the planner has already spent it.
        """
        offences: list[str] = []
        for name, entry in graph["metrics"].items():
            for field_name in ("weight_metric", "numerator_metric"):
                referenced = entry.get(field_name)
                if referenced and referenced not in registry_all:
                    offences.append(f"{name}.{field_name} -> {referenced}")
        assert not offences, "not certified metrics: " + "; ".join(offences)

    def test_no_metric_weights_itself(self, graph: dict) -> None:
        for name, entry in graph["metrics"].items():
            assert entry.get("weight_metric") != name


class TestKnownTrapsAreRecorded:
    """
    Three traps are documented elsewhere in the repo and will produce confident
    wrong findings if the planner does not know about them. Pin that they stay
    recorded, because the whole value of `cautions` is that nobody drops one.
    """

    @pytest.mark.parametrize(
        "metric, needle",
        [
            # Snapshot: summing across months double-counts people.
            ("total_subscribers", "snapshot"),
            # mrr_change_usd omits churned rows by design.
            ("expansion_mrr", "excludes churned"),
        ],
    )
    def test_caution_is_present(self, graph: dict, metric: str, needle: str) -> None:
        cautions = " ".join(graph["metrics"][metric].get("cautions") or []).lower()
        assert needle.lower() in cautions, (
            f"{metric} lost its '{needle}' caution — that trap produces a "
            "plausible-looking wrong finding."
        )

    def test_window_sensitive_traps_moved_to_the_artifact_registry(self) -> None:
        """
        The trailing churn-only period USED to be a per-metric caution here, and that
        was wrong: cautions are unconditional, so it fired on a live June diagnosis
        while the newest period was August. It was the most alarming line in an answer
        it did not apply to.

        It now lives in known_artifacts.yml, scoped to `current_month`, so it only
        surfaces when the window actually reaches it. This asserts the move happened
        rather than the trap being dropped.
        """
        from core.diagnostics.artifacts import ArtifactRegistry

        registry = ArtifactRegistry.load()
        trailing = [a for a in registry.all() if a.id == "mrr-spine-trailing-month"]
        assert trailing, "the trailing churn-only trap is recorded nowhere"
        assert trailing[0].relative.startswith("current_month"), (
            "it must be window-scoped, or it repeats the unconditional-caution bug"
        )
        assert "churn_rate" in trailing[0].metrics

        # ...and it must NOT have been left behind as an unconditional caution.
        graph_text = open(_GRAPH_PATH, encoding="utf-8").read()
        for line in graph_text.splitlines():
            if line.strip().startswith("- ") and "churn-only" in line:
                raise AssertionError(
                    "a churn-only caution is back in driver_graph.yml; it fires "
                    "regardless of window"
                )
