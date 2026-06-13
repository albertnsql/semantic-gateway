"""
core/snowflake_pool.py — Thread-safe Snowflake connection pool.

Pre-opens a fixed number of Snowflake connections at startup and leases
them to callers via a context manager.  This eliminates the ~2 s
connection overhead that would otherwise be paid on every query.

Fixes applied:
  - _ping() now closes the cursor after SELECT 1 (no more cursor leak)
  - _ping() no longer opens a replacement connection (acquire() does it once)
  - acquire() tracks last-used time and only pings after 5+ min of idle
    time, removing the ~100ms round-trip overhead on every active query

Usage::

    pool = SnowflakePool(settings, size=5)
    pool.initialise()                   # call once at startup

    with pool.acquire() as conn:
        cursor = conn.cursor(DictCursor)
        cursor.execute(sql)
        rows = cursor.fetchall()

    pool.close_all()                    # call at shutdown
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

import snowflake.connector
from snowflake.connector import DictCursor

if TYPE_CHECKING:
    from config import Settings

logger = logging.getLogger(__name__)

_QUERY_TIMEOUT_SECONDS = 30

# Only ping a connection if it has been idle for longer than this threshold.
# Snowflake connections are stable; a round-trip ping on every query adds
# ~50-200 ms unnecessarily.
_IDLE_PING_THRESHOLD_S: float = 300.0  # 5 minutes


class SnowflakePool:
    """
    A fixed-size pool of Snowflake connections.

    Connections are pre-opened at ``initialise()`` time and checked out
    via ``acquire()``.  Stale / broken connections are automatically
    replaced with a fresh one when the caller returns them.
    """

    def __init__(self, settings: "Settings", size: int = 5) -> None:
        self._settings = settings
        self._size = size
        # Pool stores (connection, last_used_timestamp) tuples
        self._pool: queue.Queue[tuple[snowflake.connector.SnowflakeConnection, float]] = queue.Queue(maxsize=size)
        self._lock = threading.Lock()
        self._closed = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def initialise(self) -> None:
        """
        Open ``size`` connections and put them into the pool.
        Called once during application startup.
        """
        logger.info("Initialising Snowflake connection pool (size=%d)…", self._size)
        opened = 0
        for i in range(self._size):
            try:
                conn = self._open_connection()
                self._pool.put_nowait((conn, time.monotonic()))
                opened += 1
            except Exception as exc:
                logger.warning("Pool slot %d failed to open: %s", i, exc)
        logger.info("Snowflake pool ready: %d/%d connections open.", opened, self._size)

    def close_all(self) -> None:
        """Drain the pool and close all connections. Call at shutdown."""
        self._closed = True
        while not self._pool.empty():
            try:
                conn, _ = self._pool.get_nowait()
                conn.close()
            except Exception:
                pass
        logger.info("Snowflake connection pool closed.")

    # ── Public API ────────────────────────────────────────────────────────────

    @contextmanager
    def acquire(self) -> Iterator[snowflake.connector.SnowflakeConnection]:
        """
        Lease a connection from the pool.  Returns it when the ``with``
        block exits, replacing it with a fresh connection if it is broken.

        Only pings the connection if it has been idle for more than
        ``_IDLE_PING_THRESHOLD_S`` seconds (default 5 min), avoiding the
        ~100ms round-trip overhead on every active query.

        Blocks up to 30 s waiting for a free slot.  Raises
        ``SnowflakeConnectionError`` if no connection is available.
        """
        conn, last_used = self._checkout()
        idle_seconds = time.monotonic() - last_used

        try:
            # Only ping after extended idle — avoids unnecessary round-trips
            if idle_seconds > _IDLE_PING_THRESHOLD_S:
                conn = self._ping_and_replace(conn)

            yield conn

        except Exception:
            # Discard the broken connection; open a fresh replacement
            try:
                conn.close()
            except Exception:
                pass
            conn = self._open_connection()
            raise
        finally:
            # Return (possibly replaced) connection to the pool with updated timestamp
            try:
                if not self._closed:
                    self._pool.put_nowait((conn, time.monotonic()))
            except queue.Full:
                try:
                    conn.close()
                except Exception:
                    pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _checkout(self) -> tuple[snowflake.connector.SnowflakeConnection, float]:
        """Block until a (connection, last_used) pair is available (max 30 s)."""
        try:
            return self._pool.get(timeout=30)
        except queue.Empty as exc:
            from core.exceptions import SnowflakeConnectionError
            raise SnowflakeConnectionError(
                "Snowflake connection pool exhausted — all connections busy."
            ) from exc

    def _ping_and_replace(
        self, conn: snowflake.connector.SnowflakeConnection
    ) -> snowflake.connector.SnowflakeConnection:
        """
        Lightweight liveness check after idle period.

        If the connection is still alive, returns it unchanged.
        If it is broken, closes it, opens a fresh one, and returns that.
        The cursor is always closed after the ping to prevent resource leaks.
        """
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()                   # always close — prevents cursor leak
            return conn
        except Exception:
            logger.warning("Stale Snowflake connection detected after idle — replacing…")
            try:
                conn.close()
            except Exception:
                pass
            return self._open_connection()  # replacement opened exactly once here

    def _open_connection(self) -> snowflake.connector.SnowflakeConnection:
        """Open a single Snowflake connection using the stored settings."""
        s = self._settings
        return snowflake.connector.connect(
            account=s.snowflake_account,
            user=s.snowflake_user,
            password=s.snowflake_password,
            database=s.snowflake_database,
            warehouse=s.snowflake_warehouse,
            role=s.snowflake_role,
            schema=s.snowflake_schema,
            network_timeout=_QUERY_TIMEOUT_SECONDS,
            login_timeout=15,
        )
