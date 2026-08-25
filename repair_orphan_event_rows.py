"""
repair_orphan_event_rows.py — Drop event rows whose subscriber does not exist.

## What went wrong

`append_monthly_data.py` loaded its "existing" universe from **Snowflake**
(`generate_month()` → `_get_sf_connection()` → `_load_existing_subscribers()`)
but appended the rows it generated to the **`output/` CSVs**. Those are two
different datastores and they had diverged: Snowflake's `RAW.SUBSCRIBERS` held a
larger, earlier generation (~37.8k subscribers) while `output/subscribers.csv`
holds 15,960.

So the 2026-06 append drew events for ~34k subscribers and wrote them next to a
subscriber table that only knew 15,960 of them. Nothing errored — the rows are
well-formed and the FK is never enforced — so they loaded cleanly and inflated
the numbers:

* `stream_sessions` June 2026 read **471,059** where the trend implied ~116k.
  The monthly "Stream Sessions" bar was 4x its true height, and
  `_session_volume_target()`'s 0–10% growth clamp had done its job on the real
  subscribers — the excess was entirely orphans layered on top.
* 16,602 orphan `payments` rows were **exactly** the 16,602 rows with a NULL
  country in `fct_payments`, because the mart gets country from a LEFT JOIN to
  `dim_subscribers`. That is 24.6% of 2026 revenue landing in an "Unknown"
  bucket the moment revenue is sliced by country.

Every orphan in every table is dated inside June 2026, which is what identifies
this as one bad append rather than long-running drift.

`append_monthly_data.py` is fixed at the source: it now reads the universe from
the same `output/` CSVs it writes to, so read and write can no longer disagree,
and `_assert_referential_integrity()` refuses to write a frame that references an
unknown subscriber. This script repairs the rows already on disk.

## Detection

A row is bad when its `subscriber_id` is absent from `output/subscribers.csv`.
That is the whole test — there is no ambiguity to resolve and no field to rewrite,
so unlike `repair_plan_history_csv.py` this script only ever deletes.

Deleting by `subscriber_id` also keeps the *secondary* references consistent:
the generator draws `session_id` for a recommendation/watchlist row from
`session_ids_by_sub[sid]`, i.e. the same subscriber's own sessions, so a dropped
subscriber takes its whole object graph with it. `--verify` re-checks that
afterwards instead of assuming it.

**Not** in scope: the `content_id` sets in `output/content_catalog.csv` and the
event tables are disjoint, which breaks the remaining `relationships` tests in
dbt. That is a separate, known, deliberate issue (see CLAUDE.md) — fixing it
means regenerating the whole dataset. This script does not touch content ids.

Idempotent: a second run finds nothing to delete.

Usage::

    python repair_orphan_event_rows.py --dry-run   # report only
    python repair_orphan_event_rows.py             # rewrite, keeping .bak files
    python repair_orphan_event_rows.py --verify    # FK check only, no writes
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(REPO_ROOT, "output")

# The subscriber table is the authority. Everything else references it.
SUBSCRIBERS_CSV = "subscribers.csv"

# Tables carrying a subscriber_id FK. `subscriptions` is listed even though it
# had zero orphans — it is the one stateful child table, and a future bad append
# could put orphans there just as easily.
CHILD_TABLES = [
    "subscriptions",
    "payments",
    "stream_sessions",
    "recommendation_events",
    "search_events",
    "user_watchlists",
    "subscription_plan_history",
    # Not loaded into DuckDB (absent from load_raw_data_to_duckdb.py's TABLES),
    # but it is a Snowflake MERGE source, so leaving it corrupt would resurrect
    # the problem if that side is ever revived.
    "subscribers_churn_updates",
]

# The column each table is dated by, used only to show that the damage sits inside
# one month. Explicit rather than "first column ending in _date", which picks
# signup_date for subscribers_churn_updates and reports the whole dataset's span.
DATE_COLUMN = {
    "subscriptions": "start_date",
    "payments": "payment_date",
    "stream_sessions": "session_start",
    "recommendation_events": "event_timestamp",
    "search_events": "search_timestamp",
    "user_watchlists": "added_timestamp",
    "subscription_plan_history": "change_date",
    "subscribers_churn_updates": "churn_date",
}

# Secondary FKs, checked by --verify after the delete. (child, column) -> (parent, column)
SECONDARY_FKS = [
    (("payments", "subscription_id"), ("subscriptions", "subscription_id")),
    (("recommendation_events", "session_id"), ("stream_sessions", "session_id")),
    (("user_watchlists", "stream_session_id"), ("stream_sessions", "session_id")),
]

# csv.field_size_limit default is 128 KB; these files are wide but not that wide.
# Raised anyway so a single long query_text can never abort a 1.7M-row stream.
csv.field_size_limit(10 * 1024 * 1024)


def _path(table: str) -> str:
    return os.path.join(OUTPUT_DIR, f"{table}.csv")


def _load_column(table: str, column: str) -> set[str]:
    """Stream one column of a CSV into a set. Returns empty set if absent."""
    path = _path(table)
    if not os.path.exists(path):
        return set()
    values: set[str] = set()
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if column not in (reader.fieldnames or []):
            return set()
        for row in reader:
            v = row.get(column)
            if v:
                values.add(v)
    return values


def _scan(table: str, valid_ids: set[str]) -> tuple[int, int, str | None, str | None]:
    """
    Count rows and orphans in one table without writing.

    Also returns the min/max of the table's date column, so the report can show
    that the damage is confined to a single month.
    """
    path = _path(table)
    if not os.path.exists(path):
        return 0, 0, None, None

    total = orphans = 0
    lo = hi = None
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        if "subscriber_id" not in fields:
            return 0, 0, None, None
        date_col = DATE_COLUMN.get(table)
        if date_col not in fields:
            date_col = None
        for row in reader:
            total += 1
            if row.get("subscriber_id") not in valid_ids:
                orphans += 1
                if date_col:
                    v = row.get(date_col) or ""
                    if v:
                        lo = v if lo is None or v < lo else lo
                        hi = v if hi is None or v > hi else hi
    return total, orphans, lo, hi


def _rewrite(table: str, valid_ids: set[str]) -> tuple[int, int]:
    """
    Rewrite the CSV without its orphan rows, keeping a .bak.

    Writes to a temp file and replaces only on success, so an interrupted run
    cannot leave a half-written CSV where the real one was.
    """
    path = _path(table)
    tmp = path + ".tmp"
    kept = dropped = 0

    with open(path, "r", newline="", encoding="utf-8") as src, open(
        tmp, "w", newline="", encoding="utf-8"
    ) as dst:
        reader = csv.DictReader(src)
        fields = reader.fieldnames or []
        writer = csv.DictWriter(dst, fieldnames=fields)
        writer.writeheader()
        for row in reader:
            if row.get("subscriber_id") in valid_ids:
                writer.writerow(row)
                kept += 1
            else:
                dropped += 1

    backup = path + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    os.replace(tmp, path)
    return kept, dropped


def _verify() -> int:
    """Re-check every FK. Returns the number of violations found."""
    valid_ids = _load_column("subscribers", "subscriber_id")
    print(f"\nVerifying against {len(valid_ids):,} subscribers.")

    violations = 0
    for table in CHILD_TABLES:
        total, orphans, _, _ = _scan(table, valid_ids)
        if total == 0:
            continue
        flag = "OK" if orphans == 0 else "FAIL"
        print(f"  [{flag:<4}] {table:<28} {orphans:>8,} orphan / {total:>10,} rows")
        violations += orphans

    print("\nSecondary references:")
    for (child, child_col), (parent, parent_col) in SECONDARY_FKS:
        parent_ids = _load_column(parent, parent_col)
        if not parent_ids:
            print(f"  [skip] {parent}.{parent_col} unavailable")
            continue
        child_ids = _load_column(child, child_col)
        dangling = child_ids - parent_ids
        flag = "OK" if not dangling else "FAIL"
        print(
            f"  [{flag:<4}] {child}.{child_col} -> {parent}.{parent_col}: "
            f"{len(dangling):,} dangling of {len(child_ids):,} distinct"
        )
        violations += len(dangling)

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted, write nothing."
    )
    parser.add_argument(
        "--verify", action="store_true", help="Run the FK checks only, write nothing."
    )
    args = parser.parse_args()

    if not os.path.exists(_path("subscribers")):
        print(f"[!] {_path('subscribers')} not found — nothing to validate against.")
        return 1

    if args.verify:
        return 0 if _verify() == 0 else 1

    valid_ids = _load_column("subscribers", "subscriber_id")
    if not valid_ids:
        print("[!] subscribers.csv has no ids — refusing to delete every row.")
        return 1
    print(f"Authoritative subscriber universe: {len(valid_ids):,} ids "
          f"({SUBSCRIBERS_CSV})")

    print(f"\n{'table':<28}{'rows':>12}{'orphans':>10}   orphan date range")
    plan: list[str] = []
    for table in CHILD_TABLES:
        total, orphans, lo, hi = _scan(table, valid_ids)
        if total == 0:
            print(f"{table:<28}{'(absent)':>12}")
            continue
        span = f"{str(lo)[:19]} .. {str(hi)[:19]}" if orphans else "-"
        print(f"{table:<28}{total:>12,}{orphans:>10,}   {span}")
        if orphans:
            plan.append(table)

    if not plan:
        print("\nNo orphans found — nothing to do.")
        return 0

    if args.dry_run:
        print(f"\n--dry-run: {len(plan)} table(s) would be rewritten. Nothing written.")
        return 0

    print()
    for table in plan:
        kept, dropped = _rewrite(table, valid_ids)
        print(f"  [fixed] {table:<28} dropped {dropped:>8,}  kept {kept:>10,}"
              f"   (backup: {os.path.basename(_path(table))}.bak)")

    remaining = _verify()
    if remaining:
        print(f"\n[!] {remaining:,} violation(s) remain — investigate before loading.")
        return 1
    print("\nAll foreign keys resolve. Reload the warehouse:")
    print("  python load_raw_data_to_duckdb.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
