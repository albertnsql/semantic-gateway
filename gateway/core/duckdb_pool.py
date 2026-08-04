"""
core/duckdb_pool.py — DuckDB execution path, interface-compatible with SnowflakePool.

Added when the Snowflake trial expired (2026-08-03) and the warehouse moved to a
local DuckDB file. It deliberately mirrors ``SnowflakePool``'s public surface —
``initialise()``, ``acquire()`` as a context manager yielding something with
``.cursor(...)``, ``close_all()`` — so ``SQLGenerator.execute_query`` and the
dashboard route work against either engine without branching per call site.

Three things about DuckDB drive the design and are not obvious:

1. **One connection, cursors per thread.** DuckDB is embedded, so there is no
   network round trip to amortise and a "pool" of file handles buys nothing. But a
   single connection object is NOT safe to use concurrently, so each caller gets
   ``conn.cursor()`` — a lightweight thread-local view over the same database.
   That is DuckDB's documented concurrency model.

2. **The connection must be read-write, and that is not a choice.** dbt-duckdb
   hardcodes ``read_only=False`` (environments/__init__.py:139,:159), and DuckDB
   refuses a second connection to the same file with a *different* config in one
   process. Since the gateway also hosts the in-process MetricFlow engine (which
   goes through dbt-duckdb), our connection is forced to match. See the comment
   block in profiles.yml.

3. **Because of (2), read-only is enforced in SQL instead.** Every statement
   passes :func:`assert_read_only` before execution. This is a stronger backstop
   than the Snowflake setup ever had: there the runtime role was ``transformer``
   (write-capable) and LLM-revised SQL on the fallback path executed unchecked —
   Audit.md findings #5 and theme A.

Usage mirrors SnowflakePool exactly::

    pool = DuckDBPool(settings)
    pool.initialise()
    with pool.acquire() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
    pool.close_all()
"""

from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

import duckdb

if TYPE_CHECKING:
    from config import Settings

logger = logging.getLogger(__name__)


# Statements that must never reach the database from the serving path. Checked
# against the whole SQL string, so a DDL keyword buried after a semicolon is
# caught as well.
_FORBIDDEN = re.compile(
    r"\b(DROP|DELETE|INSERT|UPDATE|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|MERGE|"
    r"ATTACH|DETACH|COPY|EXPORT|IMPORT|INSTALL|LOAD|PRAGMA|SET|CALL|EXECUTE)\b",
    re.IGNORECASE,
)

# A leading CTE is normal for MetricFlow output, so "starts with SELECT" is too
# strict — WITH must be allowed.
_READ_START = re.compile(r"^\s*(WITH|SELECT|DESCRIBE|SHOW|EXPLAIN)\b", re.IGNORECASE)


class DuckDBReadOnlyViolation(Exception):
    """Raised when SQL reaching the serving path is not a single read-only query."""


def _strip_sql_comments(sql: str) -> str:
    """Remove -- line and /* block */ comments so keywords cannot hide in them."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def assert_read_only(sql: str) -> None:
    """
    Reject anything that is not a single read-only statement.

    This is the substitute for a read-only database role, which DuckDB cannot give
    us here (see the module docstring). It runs on EVERY statement, including SQL
    that MetricFlow compiled — cheap insurance, and the only check standing
    between LLM-revised fallback SQL and the database.

    Raises:
        DuckDBReadOnlyViolation: If the statement is empty, multi-statement, does
            not begin as a read, or contains a write/DDL keyword.
    """
    if not sql or not sql.strip():
        raise DuckDBReadOnlyViolation("Refusing to execute empty SQL.")

    bare = _strip_sql_comments(sql)

    # Reject multiple statements. A trailing semicolon is fine; a second statement
    # after one is not.
    if [chunk for chunk in bare.split(";") if chunk.strip()][1:]:
        raise DuckDBReadOnlyViolation(
            "Refusing to execute multiple statements in one call."
        )

    if not _READ_START.match(bare):
        opening = bare.strip().split(None, 1)[0] if bare.strip() else "?"
        raise DuckDBReadOnlyViolation(
            f"Refusing to execute a statement starting with '{opening}' — "
            "only WITH/SELECT/DESCRIBE/SHOW/EXPLAIN are allowed."
        )

    # String literals can legitimately contain these words (a country called
    # 'Update'), so only look outside quoted text.
    unquoted = re.sub(r"'[^']*'", " ", bare)
    unquoted = re.sub(r'"[^"]*"', " ", unquoted)
    match = _FORBIDDEN.search(unquoted)
    if match:
        raise DuckDBReadOnlyViolation(
            f"Refusing to execute SQL containing '{match.group(1).upper()}'."
        )


class DuckDBPool:
    """
    Single-connection DuckDB accessor with a SnowflakePool-shaped interface.

    ``size`` is accepted and ignored: it exists so callers built for
    SnowflakePool can construct this without a special case. DuckDB is embedded,
    so extra connections add no concurrency — cursors do.
    """

    def __init__(self, settings: "Settings", size: int = 1) -> None:
        self._settings = settings
        self._size = size  # accepted for interface parity; DuckDB does not pool
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()
        self._closed = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def path(self) -> str:
        """Absolute path to the DuckDB file, resolved from settings."""
        return os.path.abspath(self._settings.duckdb_path)

    def initialise(self) -> None:
        """
        Open the connection and confirm the marts are present.

        Unlike SnowflakePool.initialise — which swallows every per-slot failure and
        so lets the gateway report healthy with zero usable connections
        (Audit.md, "Health lies about Snowflake") — this RAISES when the file is
        missing or the marts are not built. A DuckDB file that is not there is a
        deterministic, local, immediately-fixable condition; degrading quietly
        would only reproduce the 27-second-503 failure mode on a new engine.
        """
        db_path = self.path
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"DuckDB file not found: {db_path}. Build it with "
                "`python load_raw_data_to_duckdb.py` then `dbt run` "
                "(DBT_TARGET=duckdb)."
            )

        logger.info("Opening DuckDB at '%s'…", db_path)
        # read_only is NOT passed: it must match dbt-duckdb's hardcoded
        # read_only=False or DuckDB rejects the second connection. Writes are
        # blocked by assert_read_only() instead.
        self._conn = duckdb.connect(db_path)

        marts = self._conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'marts'"
        ).fetchone()[0]
        if not marts:
            raise RuntimeError(
                f"DuckDB at {db_path} has no tables in schema 'marts' — run "
                "`dbt run` with DBT_TARGET=duckdb before starting the gateway."
            )
        logger.info("DuckDB ready: %d table(s) in schema 'marts'.", marts)

    def close_all(self) -> None:
        """Close the connection. Named for parity with SnowflakePool."""
        self._closed = True
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        logger.info("DuckDB connection closed.")

    # ── Public API ────────────────────────────────────────────────────────────

    @contextmanager
    def acquire(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """
        Yield a thread-local cursor over the shared connection.

        Shaped like ``SnowflakePool.acquire()`` so call sites are identical, but
        there is nothing to check out and nothing to return: a DuckDB cursor is
        cheap, so it is created per call and closed on exit. Callers still invoke
        ``.cursor()`` on the yielded object — DuckDB cursors support that and
        return another cursor, which keeps the SnowflakePool call pattern working.
        """
        if self._closed or self._conn is None:
            raise RuntimeError(
                "DuckDBPool is not initialised — call initialise() first."
            )

        cursor = self._conn.cursor()
        try:
            yield cursor
        finally:
            try:
                cursor.close()
            except Exception:
                pass

    def execute(self, sql: str) -> list[dict[str, Any]]:
        """
        Run a read-only query and return row dicts.

        Column names are UPPERCASED to match the Snowflake connector's DictCursor,
        which is what every downstream consumer already expects — the dashboard
        route reads ``row["MAX_DATE"]``, and ResponseBuilder/narrative code was
        written against Snowflake's casing. Preserving that here means the
        migration does not ripple into response shaping.

        Raises:
            DuckDBReadOnlyViolation: If the statement is not a single read.
        """
        assert_read_only(sql)

        with self.acquire() as cursor:
            cursor.execute(sql)
            columns = [d[0].upper() for d in cursor.description or []]
            rows = [dict(zip(columns, record)) for record in cursor.fetchall()]
        logger.info("Query returned %d rows (DuckDB).", len(rows))
        return rows
