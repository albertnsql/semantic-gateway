"""
precompile_templates.py — Pre-compile MetricFlow SQL templates for deployment.

Runs every metric × dimension combination in settings.warmup_matrix through
SQLGenerator.generate() so the compiled, parameterized SQL templates land in
./.sql_template_cache.json. Commit that file to the repo: it ships with every
deploy, so production (Render free tier: 0.1 CPU, ephemeral disk) never has to
run the 30-45s MetricFlow subprocess at request time — and the runtime
CacheWarmer can stay disabled (DISABLE_CACHE_WARMER=true).

Run locally from the gateway/ directory (requires .env with Snowflake creds,
since MetricFlow needs a dbt profile to compile):

    cd gateway
    python precompile_templates.py

Re-run and re-commit .sql_template_cache.json after ANY dbt semantic model
change, alongside `dbt compile`.
"""

from __future__ import annotations

import sys
import time

from config import settings
from core.intent_extractor import QueryIntent, TimeRange
from core.sql_generator import SQLGenerator
from core.sql_template_cache import SQLTemplateCache

# Metrics known to NOT exist in the MetricFlow semantic manifest (kept in sync
# with cache_warmer._METRICFLOW_UNSUPPORTED). They use the governed fallback
# SQL path at query time and cannot be pre-compiled.
_METRICFLOW_UNSUPPORTED: frozenset[str] = frozenset({
    "new_subscribers",
})

# Wide time range forces MetricFlow to embed date literals, which the cache
# then parameterizes into {start_date}/{end_date} placeholders — one template
# serves every future time window.
_WIDE_RANGE = TimeRange(start_date="2000-01-01", end_date="2039-12-31", relative="all_time")


def main() -> int:
    cache = SQLTemplateCache(
        ttl_seconds=settings.sql_template_cache_ttl_seconds,
        maxsize=settings.sql_template_cache_maxsize,
        disk_path="./.sql_template_cache.json",
        refresh_on_load=True,
    )
    generator = SQLGenerator(settings=settings, pool=None, template_cache=cache)

    combos: list[tuple[str, list[str]]] = []
    for metric, dims in settings.warmup_matrix.items():
        if metric in _METRICFLOW_UNSUPPORTED:
            continue
        combos.append((metric, []))  # bare (no group-by) variant
        combos.extend((metric, [dim]) for dim in dims)

    print(f"Pre-compiling {len(combos)} metric x dimension combinations…")
    compiled = skipped = failed = 0
    started = time.perf_counter()

    for i, (metric, dims) in enumerate(combos, start=1):
        label = f"{metric} x {dims[0] if dims else '(bare)'}"
        if cache.get([metric], dims) is not None:
            skipped += 1
            print(f"  [{i}/{len(combos)}] SKIP  {label} — already cached")
            continue

        intent = QueryIntent(
            original_query=f"precompile {label}",
            metrics=[metric],
            dimensions=dims,
            time_range=_WIDE_RANGE,
            filters=[],
        )
        t0 = time.perf_counter()
        try:
            generator.generate(intent, None)
            # generate() only stores a template when MetricFlow itself succeeded
            # (fallback SQL is never cached), so verify the entry actually landed.
            if cache.get([metric], dims) is not None:
                compiled += 1
                print(f"  [{i}/{len(combos)}] OK    {label} ({time.perf_counter() - t0:.1f}s)")
            else:
                failed += 1
                print(f"  [{i}/{len(combos)}] FAIL  {label} — MetricFlow fell back, not cached")
        except Exception as exc:
            failed += 1
            print(f"  [{i}/{len(combos)}] FAIL  {label} — {exc}")

    elapsed = time.perf_counter() - started
    print(
        f"\nDone in {elapsed:.0f}s: {compiled} compiled, {skipped} already cached, {failed} failed."
        f"\nTemplates written to ./.sql_template_cache.json — commit this file."
    )
    return 1 if (compiled == 0 and skipped == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
