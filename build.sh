#!/usr/bin/env bash
#
# build.sh — Render build command for the gateway.
#
# Point Render's "Build Command" at this file:
#
#     ./build.sh
#
# Why this script exists at all: the DuckDB warehouse is a 210 MB build artifact
# that CANNOT live in git (GitHub's per-file hard limit is 100 MB, and even the
# marts-only variant is 98.5 MB — too close to rely on). It is published as a
# GitHub Release asset instead and fetched here.
#
# It also runs `dbt compile`, because `dbt_streaming_analytics/.../target/` is
# gitignored and NOT tracked — the gateway reads target/manifest.json directly for
# lineage, and the warm in-process MetricFlow engine needs
# target/semantic_manifest.json. Those artifacts have always been produced at
# build time; this just makes the step explicit and ordered correctly.
#
# Required environment variable (set in Render → Environment):
#   DUCKDB_ASSET_URL   Direct download URL of the streaming_analytics.duckdb
#                      release asset, e.g.
#                      https://github.com/<owner>/<repo>/releases/download/<tag>/streaming_analytics.duckdb
#
# Optional:
#   DUCKDB_SHA256      Expected SHA-256 of the asset. When set, a mismatch fails
#                      the build instead of shipping a truncated or wrong file —
#                      worth setting, because a half-downloaded DuckDB file fails
#                      at QUERY time with a confusing error, not at download time.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="${REPO_ROOT}/streaming_analytics.duckdb"
DBT_DIR="${REPO_ROOT}/dbt_streaming_analytics/streaming_analytics"

echo "=== 1/4 Installing Python dependencies ==="
pip install --no-cache-dir -r "${REPO_ROOT}/gateway/requirements.txt"

echo
echo "=== 2/4 Fetching the DuckDB warehouse ==="
if [[ -z "${DUCKDB_ASSET_URL:-}" ]]; then
  echo "ERROR: DUCKDB_ASSET_URL is not set." >&2
  echo "Set it in Render → Environment to the release asset URL for" >&2
  echo "streaming_analytics.duckdb. Without the warehouse every query 503s." >&2
  exit 1
fi

# --fail so an HTML 404 page is never written into the .duckdb file, --location to
# follow GitHub's redirect to the storage CDN, --retry for transient failures.
curl --fail --location --retry 3 --retry-delay 2 \
     --output "${DB_PATH}" "${DUCKDB_ASSET_URL}"

if [[ ! -s "${DB_PATH}" ]]; then
  echo "ERROR: downloaded file is empty: ${DB_PATH}" >&2
  exit 1
fi
echo "Downloaded $(du -h "${DB_PATH}" | cut -f1) to ${DB_PATH}"

if [[ -n "${DUCKDB_SHA256:-}" ]]; then
  echo "Verifying checksum…"
  actual="$(sha256sum "${DB_PATH}" | cut -d' ' -f1)"
  if [[ "${actual}" != "${DUCKDB_SHA256}" ]]; then
    echo "ERROR: checksum mismatch." >&2
    echo "  expected: ${DUCKDB_SHA256}" >&2
    echo "  actual  : ${actual}" >&2
    exit 1
  fi
  echo "Checksum OK."
else
  echo "DUCKDB_SHA256 not set — skipping integrity check."
fi

echo
echo "=== 3/4 Compiling the dbt manifests ==="
# DUCKDB_PATH must be ABSOLUTE: dbt-duckdb resolves a relative path against the
# CURRENT WORKING DIRECTORY, and the gateway's cwd at runtime is gateway/, not the
# dbt project. Exporting it absolute keeps the build and the runtime on one file.
export DUCKDB_PATH="${DB_PATH}"
export DBT_TARGET="${DBT_TARGET:-duckdb}"

cd "${DBT_DIR}"
# `dbt compile` needs a warehouse connection, which is why it runs AFTER the
# download. If it ever needs to run without the file, `dbt parse` produces both
# manifest.json and semantic_manifest.json with no connection at all.
dbt compile --profiles-dir .

for artifact in target/manifest.json target/semantic_manifest.json; do
  if [[ ! -s "${artifact}" ]]; then
    echo "ERROR: ${artifact} was not produced — the gateway needs it for" >&2
    echo "lineage resolution and for the warm MetricFlow engine." >&2
    exit 1
  fi
done
echo "Manifests written."

echo
echo "=== 4/4 Verifying the warehouse is queryable ==="
cd "${REPO_ROOT}"
python - <<'PY'
import os
import sys

import duckdb

path = os.environ["DUCKDB_PATH"]
con = duckdb.connect(path, read_only=True)
try:
    marts = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'marts'"
    ).fetchone()[0]
    if not marts:
        print(f"ERROR: no tables in schema 'marts' in {path}.", file=sys.stderr)
        print("The asset was probably built before `dbt run`.", file=sys.stderr)
        sys.exit(1)
    rows = con.execute("SELECT COUNT(*) FROM marts.fct_mrr_monthly").fetchone()[0]
    print(f"OK: {marts} marts table(s), fct_mrr_monthly has {rows:,} rows.")
finally:
    con.close()
PY

echo
echo "Build complete."
