"""
repair_plan_history_csv.py — Fix the rotated columns in subscription_plan_history.csv.

## What went wrong

`append_monthly_data.py` built plan-change rows with this key order::

    change_id, subscriber_id, change_date, old_plan, new_plan,
    change_type, old_mrr_usd, new_mrr_usd, change_reason

while the header written by the original generator is::

    change_id, subscriber_id, old_plan, new_plan, old_mrr_usd,
    new_mrr_usd, change_type, change_date, change_reason

`df.to_csv(mode="a")` appends POSITIONALLY, so six fields were rotated in every
monthly append: dates landed in `old_plan`, prices in `change_type`, and the
change direction in `old_mrr_usd`. Nothing errored — the row width is identical —
so the corruption loaded cleanly into Snowflake and was only caught when DuckDB
tried to cast `old_mrr_usd` to DECIMAL and hit the string "basic".

The generator is fixed (append_to_csv now reindexes to the on-disk header). This
script repairs the rows already written.

## Detection

A bad row is identified structurally, not by position: in a correct row field 2
is a plan name, in a bad row it is an ISO date. Rows are only rewritten when the
rotation produces a self-consistent record (plan names in the plan fields,
numbers in the money fields), so a row that matches neither shape is reported and
left alone rather than mangled further.

Idempotent: running it twice is a no-op because repaired rows no longer match the
bad shape.

Usage::

    python repair_plan_history_csv.py --dry-run    # report only
    python repair_plan_history_csv.py             # rewrite, keeping a .bak
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(REPO_ROOT, "output", "subscription_plan_history.csv")

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
PLANS = {"basic", "standard", "premium"}

# Header (correct) order.
GOOD = [
    "change_id", "subscriber_id", "old_plan", "new_plan",
    "old_mrr_usd", "new_mrr_usd", "change_type", "change_date", "change_reason",
]
# Order the appender actually wrote.
BAD = [
    "change_id", "subscriber_id", "change_date", "old_plan",
    "new_plan", "change_type", "old_mrr_usd", "new_mrr_usd", "change_reason",
]


def looks_bad(row: list[str]) -> bool:
    """A rotated row has an ISO date where the header expects ``old_plan``."""
    return len(row) == len(GOOD) and bool(ISO_DATE.match(row[2]))


def repair(row: list[str]) -> list[str] | None:
    """
    Re-map a rotated row into header order.

    Returns ``None`` if the result would not be self-consistent, so genuinely
    malformed rows are surfaced instead of silently rewritten.
    """
    record = dict(zip(BAD, row))
    fixed = [record[col] for col in GOOD]

    plausible = (
        record["old_plan"].lower() in PLANS
        and record["new_plan"].lower() in PLANS
        and NUMBER.match(record["old_mrr_usd"])
        and NUMBER.match(record["new_mrr_usd"])
        and ISO_DATE.match(record["change_date"])
    )
    return fixed if plausible else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=DEFAULT_CSV, help="Path to subscription_plan_history.csv")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"File not found: {args.csv}", file=sys.stderr)
        return 1

    with open(args.csv, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)

    if header != GOOD:
        print(f"Unexpected header, refusing to touch the file:\n  {header}", file=sys.stderr)
        return 1

    repaired, unrepairable, clean = [], [], 0
    for line_no, row in enumerate(rows, start=2):
        if not looks_bad(row):
            repaired.append(row)
            clean += 1
            continue
        fixed = repair(row)
        if fixed is None:
            unrepairable.append((line_no, row))
            repaired.append(row)
        else:
            repaired.append(fixed)

    changed = len(rows) - clean - len(unrepairable)
    print(f"File            : {args.csv}")
    print(f"Total rows      : {len(rows):,}")
    print(f"Already correct : {clean:,}")
    print(f"Rotated -> fixed: {changed:,}")
    print(f"Unrepairable    : {len(unrepairable):,}")
    for line_no, row in unrepairable[:5]:
        print(f"  line {line_no}: {row}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0
    if changed == 0:
        print("\nNothing to repair.")
        return 0

    backup = args.csv + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(args.csv, backup)
        print(f"\nBackup written  : {backup}")
    else:
        print(f"\nBackup exists   : {backup} (kept)")

    with open(args.csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(repaired)
    print(f"Rewrote {len(repaired):,} rows in header order.")
    print("Re-run: python load_raw_data_to_duckdb.py --tables subscription_plan_history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
