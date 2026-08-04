"""
load_raw_data_to_duckdb.py — Load the generated CSVs into a local DuckDB file.

The DuckDB counterpart of load_raw_data_to_snowfalke.py, written when the
Snowflake trial expired. Same contract, so the dbt project does not care which
one ran:

  * reads the SAME ten tables from ``output/`` (not ``raw_tables/`` — ``output/``
    is what append_monthly_data.py appends to, so it is the current data)
  * creates them in schema ``raw``, matching ``models/staging/_sources.yml``
    (``database: streaming_analytics`` / ``schema: raw``)

The database FILENAME is load-bearing: DuckDB derives the catalog name from it,
so ``streaming_analytics.duckdb`` makes the sources' ``database:
streaming_analytics`` resolve, and the dashboard route's hand-written
``STREAMING_ANALYTICS.marts.<table>`` three-part names keep working unchanged
(DuckDB matches identifiers case-insensitively).

Type inference: ``sample_size=-1`` scans every row rather than the default
sample. stream_sessions.csv is ~346 MB and a partial sample mistypes late-file
columns — which would surface much later as a dbt cast error rather than here.

Usage::

    python load_raw_data_to_duckdb.py                  # default output path
    python load_raw_data_to_duckdb.py --db /tmp/sa.duckdb
    python load_raw_data_to_duckdb.py --tables subscribers payments
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import duckdb

# Table list and order mirror load_raw_data_to_snowfalke.py exactly.
TABLES = [
    "content_catalog",
    "content_genre_bridge",
    "payments",
    "recommendation_events",
    "search_events",
    "stream_sessions",
    "subscribers",
    "subscriptions",
    "subscription_plan_history",
    "user_watchlists",
]

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CSV_DIR = os.path.join(REPO_ROOT, "output")

# Keep this filename in sync with DUCKDB_PATH in gateway/.env and the duckdb
# target in dbt profiles.yml — see the module docstring on why it matters.
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "streaming_analytics.duckdb")

RAW_SCHEMA = "raw"


def load_table(conn: duckdb.DuckDBPyConnection, table: str, csv_dir: str) -> int | None:
    """
    Replace ``raw.<table>`` with the contents of ``<csv_dir>/<table>.csv``.

    Returns the row count, or ``None`` if the CSV is missing (skipped, matching
    the Snowflake loader's behaviour rather than failing the whole run).
    """
    csv_path = os.path.join(csv_dir, f"{table}.csv")
    if not os.path.exists(csv_path):
        print(f"  ! File not found: {csv_path} — skipping")
        return None

    size_mb = os.path.getsize(csv_path) / (1024 * 1024)
    print(f"  - {table} ({size_mb:,.1f} MB)...", end=" ", flush=True)
    started = time.perf_counter()

    # Forward slashes: DuckDB treats a backslash as an escape inside a string
    # literal, so a raw Windows path silently breaks the read.
    duck_path = csv_path.replace("\\", "/")
    conn.execute(
        f"""
        CREATE OR REPLACE TABLE {RAW_SCHEMA}.{table} AS
        SELECT * FROM read_csv_auto('{duck_path}', header = true, sample_size = -1)
        """
    )
    rows = conn.execute(f"SELECT COUNT(*) FROM {RAW_SCHEMA}.{table}").fetchone()[0]
    print(f"{rows:,} rows in {time.perf_counter() - started:,.1f}s")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("DUCKDB_PATH", DEFAULT_DB_PATH),
        help="Path to the DuckDB file to create/update.",
    )
    parser.add_argument(
        "--csv-dir",
        default=CSV_DIR,
        help="Directory holding the <table>.csv files (default: output/).",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        default=TABLES,
        help="Subset of tables to load (default: all ten).",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.csv_dir):
        print(f"CSV directory does not exist: {args.csv_dir}", file=sys.stderr)
        return 1

    unknown = [t for t in args.tables if t not in TABLES]
    if unknown:
        print(f"Unknown table(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    print(f"DuckDB file : {args.db}")
    print(f"CSV source  : {args.csv_dir}")
    print(f"Loading {len(args.tables)} table(s) into schema '{RAW_SCHEMA}'...\n")

    conn = duckdb.connect(args.db)
    try:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {RAW_SCHEMA}")
        loaded, skipped = 0, 0
        for table in args.tables:
            if load_table(conn, table, args.csv_dir) is None:
                skipped += 1
            else:
                loaded += 1
    finally:
        conn.close()

    db_mb = os.path.getsize(args.db) / (1024 * 1024)
    print(f"\nDone. {loaded} table(s) loaded, {skipped} skipped.")
    print(f"Database size: {db_mb:,.1f} MB")
    if skipped:
        print("Some tables were skipped — dbt will fail on the missing sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
