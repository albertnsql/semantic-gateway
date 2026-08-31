"""
scratch/try_diagnosis.py — run a diagnosis locally without the LLM or the frontend.

A dev aid, not part of the shipped app. Builds the real services against the real
DuckDB file and drives the diagnostic graph directly, skipping intent extraction —
so a failure here is the graph, the warehouse or the driver graph, never the model
and never the UI. That separation is the point: the full path has four layers that
can fail and this narrows it to one.

    cd gateway
    python scratch/try_diagnosis.py
    python scratch/try_diagnosis.py --metric churn_rate --months 6
    python scratch/try_diagnosis.py --metric total_revenue --filter country=DE

Filter VALUES must match the warehouse, which stores ISO codes: `country=DE`, not
`country=Germany`. The live extractor emits "Germany" for "why is revenue lower in
Germany", which filters to zero rows — worth knowing before blaming the graph.

Stop the gateway first if it is running: DuckDB's writer lock is process-exclusive.
The first diagnosis pays the ~43s warm MetricFlow engine build; later ones in the
same process take under a second.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import types
from datetime import date

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one diagnosis against local DuckDB.")
    ap.add_argument("--metric", default="total_revenue")
    ap.add_argument("--question", default="")
    ap.add_argument("--start", default="", help="target window start, YYYY-MM-DD")
    ap.add_argument("--end", default="", help="target window end, YYYY-MM-DD")
    ap.add_argument("--months", type=int, default=0,
                    help="use the last N whole months instead of --start/--end")
    ap.add_argument("--filter", action="append", default=[],
                    metavar="COL=VALUE", help="repeatable; values must match the warehouse")
    ap.add_argument("--dimensions", type=int, default=3)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )

    from config import settings
    settings.diagnostics_enabled = True

    from core.duckdb_pool import DuckDBPool, resolve_duckdb_path
    from core.intent_extractor import FilterClause, TimeRange
    from core.manifest_parser import ManifestParser
    from core.metric_registry import MetricRegistry
    from core.semantic_validator import SemanticValidator
    from core.sql_generator import SQLGenerator
    from core.sql_template_cache import SQLTemplateCache
    from core.diagnostics.windows import trailing_months
    import api.routes.query as route

    os.environ["DUCKDB_PATH"] = resolve_duckdb_path(settings.duckdb_path)
    print(f"warehouse : {os.environ['DUCKDB_PATH']}")
    if not os.path.exists(os.environ["DUCKDB_PATH"]):
        print("  MISSING - rebuild it: python load_raw_data_to_duckdb.py && dbt run")
        return 1

    parser = ManifestParser(); parser.load(settings.manifest_path)
    registry = MetricRegistry()
    registry.load(settings.metrics_path, settings.semantic_models_path, parser)
    pool = DuckDBPool(settings); pool.initialise()
    template_cache = SQLTemplateCache(
        settings.sql_template_cache_ttl_seconds, settings.sql_template_cache_path
    )
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        semantic_validator=SemanticValidator(registry),
        sql_generator=SQLGenerator(settings=settings, pool=pool,
                                   template_cache=template_cache),
        metric_registry=registry,
        query_cache=None,
    ))

    if args.months:
        window = trailing_months(date.today(), args.months, inclusive=False)
        time_range = TimeRange(start_date=str(window.start), end_date=str(window.end))
    elif args.start and args.end:
        time_range = TimeRange(start_date=args.start, end_date=args.end)
    else:
        time_range = None   # the route defaults to the last N whole months

    filters = []
    for raw in args.filter:
        if "=" not in raw:
            print(f"  ignoring malformed --filter {raw!r}; expected COL=VALUE")
            continue
        column, value = raw.split("=", 1)
        filters.append(FilterClause(column=column, operator="eq", value=value))

    question = args.question or f"why did {args.metric} change"
    intent = types.SimpleNamespace(
        metrics=[args.metric], dimensions=[], filters=filters,
        time_range=time_range, query_type="diagnostic_query",
    )

    class _Options:
        include_sql = False
        max_rows = 100

    body = types.SimpleNamespace(query=question, options=_Options())

    print(f"question  : {question}")
    print(f"metric    : {args.metric}")
    print("building the warm MetricFlow engine on first probe (~43s cold)...\n")

    started = time.perf_counter()
    result = route._run_diagnosis(
        body, intent, types.SimpleNamespace(app=app), "local"
    )
    elapsed = (time.perf_counter() - started) * 1000

    if result is None:
        print("FELL THROUGH to out_of_scope. Common causes:")
        print("  - the metric has no driver_graph entry")
        print("  - langgraph is not installed (pip install -r requirements.txt)")
        print("  - an exception mid-run; re-run with --verbose to see it")
        return 1

    ok = sum(1 for e in result["evidence"] if not e["error"])
    print(f"{result['target_window']}  vs  {result['comparison_window']}")
    print(f"{len(result['evidence'])} probe(s), {ok} ok, {elapsed:.0f} ms")
    print(f"decomposed by: {', '.join(result['dimensions_examined']) or '(none)'}")
    print("-" * 74)
    for line in result["answer"].splitlines():
        print(line)
    print("-" * 74)

    failed = [e for e in result["evidence"] if e["error"]]
    if failed:
        print(f"\n{len(failed)} probe(s) failed:")
        for e in failed:
            print(f"  {e['id']} {e['label']}\n      {e['error'][:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
