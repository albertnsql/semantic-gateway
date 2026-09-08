"""
core/metricflow_engine.py — one warm, in-process MetricFlow engine.

Why this exists
---------------
`mf query --explain` as a subprocess costs 18–34 s per compile, and essentially
all of that is process startup: importing dbt + MetricFlow and parsing the
semantic manifest. The compile itself is milliseconds. Measured on this project:

    subprocess per compile   : 18.5 s (batch) … 33.9 s (serial, loaded machine)
    warm in-process explain(): 0.021 s mean, 0.05 s worst of 20

That ~1000x difference is why a cache miss used to be a 30–50 s cliff. Holding
one engine for the lifetime of the process turns a miss into a rounding error.

Cost of holding it: ~100 MB resident and ~13–22 s of extra startup, both paid
once. On a 512 MB instance that is affordable but not free — see
`Settings.metricflow_in_process` to turn it off.

Design notes
------------
* **Parity by construction.** :meth:`explain_argv` takes the *same argv list*
  ``SQLGenerator.format_mf_query()`` builds for the subprocess and translates it
  into engine parameters. One source of truth for metrics, dimensions, time
  constraints, where-clauses and limits — the two paths cannot drift.
* **Never fatal.** :meth:`try_build` returns ``None`` on any failure, so the
  caller silently keeps using the subprocess path.
* **Serialised.** ``MetricFlowEngine`` makes no thread-safety guarantee and the
  gateway calls it from ``anyio.to_thread`` workers, so every explain holds a
  lock. At ~20 ms per call this is not a meaningful bottleneck.
"""

from __future__ import annotations

import datetime
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


class WarmMetricFlowEngine:
    """A long-lived MetricFlow engine that compiles SQL without a subprocess."""

    def __init__(self, cli_configuration: Any, engine: Any, request_cls: Any) -> None:
        self._cfg = cli_configuration
        self._engine = engine
        self._request_cls = request_cls
        self._lock = threading.Lock()
        self.explain_count = 0

    # ────────────────────────────────────────────────────────────── construction

    @classmethod
    def try_build(
        cls, dbt_project_dir: str, dbt_profiles_dir: str
    ) -> Optional["WarmMetricFlowEngine"]:
        """
        Build the engine, or return ``None`` if anything goes wrong.

        Failure is expected and non-fatal in environments where the dbt project
        is not deployed alongside the gateway — the caller falls back to the
        subprocess path.
        """
        import os
        import pathlib
        import time

        started = time.perf_counter()
        project = pathlib.Path(dbt_project_dir).resolve()
        profiles = pathlib.Path(dbt_profiles_dir).resolve()

        if not (project / "dbt_project.yml").exists():
            logger.warning(
                "In-process MetricFlow disabled: no dbt_project.yml under '%s'. "
                "Falling back to the `mf` subprocess.", project,
            )
            return None

        # The dbt adapter reads these; set them before the manifest is loaded.
        os.environ.setdefault("DBT_PROJECT_DIR", str(project))
        os.environ.setdefault("DBT_PROFILES_DIR", str(profiles))

        try:
            from dbt_metricflow.cli.cli_configuration import CLIConfiguration
            from metricflow.engine.metricflow_engine import MetricFlowQueryRequest

            cfg = CLIConfiguration()
            cfg.setup(
                dbt_profiles_path=profiles,
                dbt_project_path=project,
                configure_file_logging=False,
            )
            engine = cfg.mf
        except Exception as exc:
            logger.warning(
                "In-process MetricFlow unavailable (%s) — falling back to the "
                "`mf` subprocess. Compiles will cost ~30 s on a cache miss.", exc,
            )
            return None

        instance = cls(cfg, engine, MetricFlowQueryRequest)
        logger.info(
            "✓ Warm MetricFlow engine ready in %.1fs — cache misses now compile "
            "in-process instead of paying a ~30s subprocess.",
            time.perf_counter() - started,
        )
        return instance

    # ───────────────────────────────────────────────────────────────── compiling

    @staticmethod
    def _parse_argv(mf_command: list[str]) -> dict:
        """
        Translate ``["mf","query","--metrics","mrr","--group-by","x", …]`` into
        MetricFlowQueryRequest kwargs.

        Reusing the subprocess argv is deliberate: it keeps a single source of
        truth for how an intent becomes a MetricFlow query.
        """
        kwargs: dict = {}
        i = 0
        while i < len(mf_command):
            token = mf_command[i]
            if not token.startswith("--"):
                i += 1
                continue
            if token == "--explain":
                i += 1
                continue
            value = mf_command[i + 1] if i + 1 < len(mf_command) else None
            if value is None:
                break
            if token == "--metrics":
                kwargs["metric_names"] = [m for m in value.split(",") if m]
            elif token == "--group-by":
                kwargs["group_by_names"] = [d for d in value.split(",") if d]
            elif token == "--start-time":
                kwargs["time_constraint_start"] = datetime.datetime.fromisoformat(value)
            elif token == "--end-time":
                kwargs["time_constraint_end"] = datetime.datetime.fromisoformat(value)
            elif token == "--where":
                kwargs["where_constraints"] = [value]
            elif token == "--order":
                # Parity with format_mf_query's --order. Without this the WARM
                # engine -- the PRIMARY path -- would silently drop the ordering
                # while the subprocess honoured it, so a superlative answer
                # would depend on which rung happened to compile it.
                kwargs["order_by_names"] = [o for o in value.split(",") if o]
            elif token == "--limit":
                try:
                    kwargs["limit"] = int(value)
                except ValueError:
                    logger.warning("Ignoring non-integer --limit %r.", value)
            i += 2
        return kwargs

    def explain_argv(self, mf_command: list[str]) -> str:
        """
        Compile *mf_command* to SQL in-process.

        Args:
            mf_command: The argv list produced by ``format_mf_query``.

        Returns:
            The compiled SQL string.

        Raises:
            Exception: Whatever MetricFlow raises. Callers should treat any
                failure as "fall back to the subprocess", not as fatal.
        """
        kwargs = self._parse_argv(mf_command)
        if not kwargs.get("metric_names"):
            raise ValueError(f"No metrics parsed from argv: {mf_command!r}")

        request = self._request_cls.create(**kwargs)
        with self._lock:
            result = self._engine.explain(request)
            self.explain_count += 1

        # `.without_descriptions` is what the CLI emits unless
        # --show-sql-descriptions is passed, and format_mf_query never passes it.
        # Using the annotated `.sql` instead would embed MetricFlow plan comments
        # such as "-- Constrain Time Range to [2000-01-01T00:00:00, …]" — whose ISO
        # timestamps then get picked up by parameterize_sql_dates and corrupt the
        # cached template. Match the subprocess exactly.
        return result.sql_statement.without_descriptions.sql
