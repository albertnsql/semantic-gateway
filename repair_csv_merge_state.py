"""
repair_csv_merge_state.py — Collapse the duplicate rows the CSV append path left behind.

## What went wrong

`upload_to_snowflake()` distinguishes INSERT payloads from MERGE payloads
(`_MERGE_TABLES`, `_CHURN_UPDATE_TABLE`). `append_to_csv()` did not — it appended
everything. Two consequences, both in `generate_month()`'s output:

1. **`subscriptions`** receives `new_subscription_rows + churned_subscription_updates`.
   The churn half is an UPDATE (status → cancelled, end_date set) for a subscription
   that already exists, so appending it wrote a SECOND row for the same
   `subscription_id`. This is the failing `unique_stg_subscriptions_subscription_id`
   dbt test, and it is not cosmetic: `int_subscription_periods` expands one period
   set per subscription row, so a duplicated subscription contributes two
   overlapping period sets to `fct_mrr_monthly`.

2. **`subscribers_churn_updates`** went to its own file and *never* to
   `subscribers.csv`. So `dim_subscribers` kept reporting churned subscribers as
   `active`, and — because the next month's append reads `subscribers.csv` to decide
   who is eligible to churn — the same people were churned again. With
   `random.seed(42)` fixed at module load and a near-identical active list,
   `random.sample()` re-picked 313 of the same 316.

`append_monthly_data.py` is fixed at the source (`_CSV_UPSERT_KEYS` and
`_apply_churn_to_subscribers()`). This script reconciles what is already on disk.

## Reconciliation rules

**Duplicate `subscription_id` → keep the EARLIEST cancellation.** A subscription
cannot be cancelled twice; the first churn is the one that happened, and every later
"cancellation" of the same subscription is the artefact. If no copy is cancelled,
the single surviving row is the active one.

**Churn state → apply the EARLIEST churn per subscriber** onto `subscribers.csv`
(`subscription_status`, `churn_date`, `churn_reason`), for the same reason.

**`subscribers_churn_updates.csv` → one row per subscriber**, the earliest, so the
file stops being a log of repeated churns and becomes the MERGE payload it is
meant to be.

Idempotent: after a run there are no duplicate subscription_ids and no subscriber
with a churn row whose status is still `active`, so a second run reports nothing.

Usage::

    python repair_csv_merge_state.py --dry-run   # report only
    python repair_csv_merge_state.py             # rewrite, keeping .bak files
    python repair_csv_merge_state.py --verify    # check only, no writes
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(REPO_ROOT, "output")

CHURN_COLUMNS = ["subscription_status", "churn_date", "churn_reason"]


def _path(table: str) -> str:
    return os.path.join(OUTPUT_DIR, f"{table}.csv")


def _read(table: str, **kw) -> pd.DataFrame:
    return pd.read_csv(_path(table), low_memory=False, **kw)


def _backup(table: str) -> None:
    path = _path(table)
    backup = path + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)


def _write(table: str, df: pd.DataFrame) -> None:
    """Write in place via a temp file, preserving the on-disk column order."""
    path = _path(table)
    header = pd.read_csv(path, nrows=0).columns.tolist()
    _backup(table)
    tmp = path + ".tmp"
    df[header].to_csv(tmp, index=False)
    os.replace(tmp, path)


def _collapse(dups: pd.DataFrame) -> pd.DataFrame:
    """
    Reduce each duplicated subscription_id to its one true row.

    Cancelled rows win over active ones (the subscription did end), and among
    cancellations the earliest end_date wins — a subscription cannot be cancelled
    twice, so every later "cancellation" is the artefact.
    """
    ranked = dups.assign(
        _cancelled=dups["status"].astype(str).str.lower().eq("cancelled"),
        _end=dups["end_date"].astype(str),
    ).sort_values(["_cancelled", "_end"], ascending=[False, True])
    return ranked.drop_duplicates("subscription_id", keep="first").drop(
        columns=["_cancelled", "_end"]
    )


def _collapse_subscriptions(dry_run: bool) -> int:
    df = _read("subscriptions")
    dup_mask = df["subscription_id"].duplicated(keep=False)
    dup_ids = df.loc[dup_mask, "subscription_id"].nunique()
    excess = int(dup_mask.sum()) - dup_ids

    print(f"\nsubscriptions.csv: {len(df):,} rows, "
          f"{df['subscription_id'].nunique():,} distinct subscription_id")
    if excess <= 0:
        print("  no duplicate subscription_id — nothing to collapse")
        return 0
    print(f"  {dup_ids:,} subscription_id(s) duplicated, {excess:,} excess row(s)")

    keep_singles = df[~dup_mask]
    collapsed = _collapse(df[dup_mask])
    result = pd.concat([keep_singles, collapsed], ignore_index=True)

    assert result["subscription_id"].is_unique, "collapse did not produce unique ids"
    assert set(result["subscription_id"]) == set(df["subscription_id"]), (
        "collapse lost a subscription_id"
    )

    print(f"  -> {len(result):,} rows, one per subscription_id")
    if dry_run:
        return excess
    _write("subscriptions", result)
    print("  [fixed] subscriptions.csv (backup: subscriptions.csv.bak)")
    return excess


def _earliest_churn() -> pd.DataFrame:
    """One row per subscriber from the churn payload, the earliest churn_date."""
    df = _read("subscribers_churn_updates")
    df = df[df["churn_date"].notna()]
    df = df.sort_values("churn_date").drop_duplicates("subscriber_id", keep="first")
    return df


def _apply_churn(dry_run: bool) -> int:
    if not os.path.exists(_path("subscribers_churn_updates")):
        print("\nsubscribers_churn_updates.csv absent — no churn state to apply")
        return 0

    payload = _earliest_churn()
    subs = _read("subscribers")
    raw_rows = len(_read("subscribers_churn_updates"))

    print(f"\nsubscribers_churn_updates.csv: {raw_rows:,} rows, "
          f"{len(payload):,} distinct subscribers "
          f"({raw_rows - len(payload):,} repeat churn row(s))")

    indexed = subs.set_index("subscriber_id")
    targets = payload.set_index("subscriber_id").index.intersection(indexed.index)
    stale = indexed.loc[targets, "subscription_status"].astype(str).str.lower() == "active"
    n_stale = int(stale.sum())

    print(f"subscribers.csv: {n_stale:,} of {len(targets):,} churned subscribers "
          f"still marked active")
    if n_stale == 0 and raw_rows == len(payload):
        print("  already reconciled — nothing to do")
        return 0

    if dry_run:
        return n_stale

    src = payload.set_index("subscriber_id")
    for col in CHURN_COLUMNS:
        if col in src.columns:
            indexed.loc[targets, col] = src.loc[targets, col]
    _write("subscribers", indexed.reset_index())
    print(f"  [fixed] subscribers.csv — churn state applied to {len(targets):,} rows "
          f"(backup: subscribers.csv.bak)")

    if raw_rows != len(payload):
        _write("subscribers_churn_updates", payload)
        print(f"  [fixed] subscribers_churn_updates.csv — deduplicated to "
              f"{len(payload):,} rows (backup: subscribers_churn_updates.csv.bak)")
    return n_stale


def _verify() -> int:
    problems = 0

    df = _read("subscriptions", usecols=["subscription_id"])
    dupes = int(df["subscription_id"].duplicated().sum())
    print(f"  [{'OK  ' if not dupes else 'FAIL'}] subscriptions: "
          f"{dupes:,} duplicate subscription_id")
    problems += dupes

    if os.path.exists(_path("subscribers_churn_updates")):
        payload = _earliest_churn()
        subs = _read("subscribers", usecols=["subscriber_id", "subscription_status"])
        merged = subs.merge(payload[["subscriber_id"]], on="subscriber_id", how="inner")
        stale = int(
            (merged["subscription_status"].astype(str).str.lower() == "active").sum()
        )
        print(f"  [{'OK  ' if not stale else 'FAIL'}] subscribers: "
              f"{stale:,} churned subscriber(s) still marked active")
        problems += stale

        raw_rows = len(_read("subscribers_churn_updates", usecols=["subscriber_id"]))
        repeats = raw_rows - len(payload)
        print(f"  [{'OK  ' if not repeats else 'FAIL'}] churn payload: "
              f"{repeats:,} repeat churn row(s)")
        problems += repeats

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    parser.add_argument("--verify", action="store_true", help="Check only, write nothing.")
    args = parser.parse_args()

    if not os.path.exists(_path("subscriptions")) or not os.path.exists(_path("subscribers")):
        print("[!] output/subscriptions.csv or output/subscribers.csv missing.")
        return 1

    if args.verify:
        print("Verifying CSV merge state:")
        return 0 if _verify() == 0 else 1

    excess = _collapse_subscriptions(args.dry_run)
    stale = _apply_churn(args.dry_run)

    if args.dry_run:
        print(f"\n--dry-run: would collapse {excess:,} excess subscription row(s) "
              f"and reconcile {stale:,} churn state(s). Nothing written.")
        return 0

    print("\nVerifying:")
    remaining = _verify()
    if remaining:
        print(f"\n[!] {remaining:,} problem(s) remain.")
        return 1
    print("\nMerge state reconciled. Reload the warehouse:")
    print("  python load_raw_data_to_duckdb.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
