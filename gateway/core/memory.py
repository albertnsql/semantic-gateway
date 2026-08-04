"""
core/memory.py — Resident memory (RSS) sampling, rendered into the gateway logs.

Why this module exists: the gateway ships to a 512 MB Render instance and the
two most expensive things it holds are known quantities — the warm MetricFlow
engine (measured 150 -> 251 MB, i.e. +101 MB) and the two disk-backed caches
(`query_cache_maxsize` x ~8 KB, `sql_template_cache_maxsize` x ~4 KB). Those
numbers are documented in config.py but were never *observed* at runtime, so an
OOM restart on Render looked identical in the logs to any other restart.

Design notes:

* **psutil is optional, on purpose.** It is the best source, but the fallbacks
  (`/proc/self/statm` on Linux, `GetProcessMemoryInfo` on Windows) cover both the
  deploy target and local dev. A missing wheel degrades the log line, never the
  process.
* **Every public function is best-effort and never raises.** Memory logging must
  not be able to fail a request; `rss_mb()` returns ``None`` when unavailable and
  callers render "n/a".
* **No config import.** Callers inject the limit via :func:`configure` (from
  ``lifespan``), keeping this module dependency-free and testable in isolation —
  the same injection style the other ``core/`` services use.

The limit is auto-detected from the cgroup when not configured, which is what
makes the percentage meaningful on Render: the container ceiling is the number
that triggers the OOM kill, not the host's total RAM.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_MB = 1024 * 1024

# Peak RSS observed in this process. Sampled, so a spike between two samples is
# invisible — still the cheapest way to distinguish "we are sitting near the
# ceiling" from "we touched it once during the MetricFlow build".
_peak_lock = threading.Lock()
_peak_mb: float = 0.0

# Resolved lazily on first read so import order never matters.
_reader: Optional[Callable[[], Optional[int]]] = None
_source: str = "unresolved"
_reader_lock = threading.Lock()

# Injected by configure(); None means "auto-detect from the cgroup".
_configured_limit_mb: Optional[float] = None
_warn_pct: float = 85.0


# ──────────────────────────────────────────────────────────── RSS backends

def _rss_via_psutil() -> Optional[int]:
    """Preferred backend — portable and cheap (a cached handle per process)."""
    try:
        import psutil  # optional dependency
    except Exception:
        return None
    try:
        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


def _rss_via_procfs() -> Optional[int]:
    """Linux fallback — this is the path that runs on Render if psutil is absent."""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return None


def _rss_via_windows() -> Optional[int]:
    """
    Windows fallback — WorkingSetSize is the closest analogue to RSS.

    Kept so local development on Windows reports the same shape of number as
    production, rather than silently logging "n/a" for every line.

    ``restype``/``argtypes`` are declared explicitly and are NOT optional:
    ``GetCurrentProcess`` returns the pseudo-handle ``(HANDLE)-1``, and with
    ctypes' default ``c_int`` restype that gets truncated to ``0xFFFFFFFF`` on
    64-bit Python. The call then fails with a FALSE return and a zeroed struct —
    no exception — so the backend would look "unavailable" instead of broken.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return None
        return int(counters.WorkingSetSize)
    except Exception:
        return None


_BACKENDS: tuple[tuple[str, Callable[[], Optional[int]]], ...] = (
    ("psutil", _rss_via_psutil),
    ("procfs", _rss_via_procfs),
    ("windows", _rss_via_windows),
)


def _safe(backend: Callable[[], Optional[int]]) -> Callable[[], Optional[int]]:
    """
    Wrap a backend so a later failure returns ``None`` instead of raising.

    A backend can work at resolution time and fail afterwards (a container losing
    access to ``/proc``, a psutil handle going stale). Since every sample happens
    on a live code path, the wrapper is what keeps that a log-quality problem.
    """

    def _read() -> Optional[int]:
        try:
            return backend()
        except Exception:
            return None

    return _read


def _resolve_reader() -> Callable[[], Optional[int]]:
    """Pick the first backend that actually returns a number, and remember it."""
    global _reader, _source

    if _reader is not None:
        return _reader

    with _reader_lock:
        if _reader is not None:  # another thread resolved while we waited
            return _reader
        for name, backend in _BACKENDS:
            # The shipped backends swallow their own errors, but probe defensively
            # anyway: this function runs inside the request path via rss_mb(), so an
            # exception escaping here would turn memory telemetry into an outage.
            try:
                probe = backend()
            except Exception as exc:
                logger.debug("Memory backend %s unusable: %s", name, exc)
                continue
            if probe is not None:
                _reader, _source = _safe(backend), name
                logger.debug("Memory sampling backend: %s", name)
                return _reader
        _reader, _source = (lambda: None), "unavailable"
        logger.warning(
            "No RSS backend available (psutil not installed and no platform "
            "fallback) — memory will log as 'n/a'."
        )
        return _reader


# ──────────────────────────────────────────────────────────── configuration

def configure(limit_mb: Optional[float] = None, warn_pct: float = 85.0) -> None:
    """
    Inject the memory ceiling used for percentage reporting.

    Args:
        limit_mb: Host/container memory limit in MB. ``None`` or ``0`` means
            auto-detect from the cgroup (correct on Render), falling back to no
            percentage at all rather than guessing.
        warn_pct: Percentage of the limit above which status lines are logged at
            WARNING instead of INFO.
    """
    global _configured_limit_mb, _warn_pct
    _configured_limit_mb = float(limit_mb) if limit_mb else None
    _warn_pct = float(warn_pct)


def reset_for_tests() -> None:
    """Clear resolved backend, peak and configuration. Test-support only."""
    global _reader, _source, _peak_mb, _configured_limit_mb, _warn_pct
    with _reader_lock:
        _reader, _source = None, "unresolved"
    with _peak_lock:
        _peak_mb = 0.0
    _configured_limit_mb = None
    _warn_pct = 85.0


# ──────────────────────────────────────────────────────────── public reads

def rss_mb() -> Optional[float]:
    """
    Current resident set size in MB, or ``None`` if it cannot be sampled.

    Also maintains the process high-water mark as a side effect, so any caller
    that logs RSS keeps :func:`peak_mb` fresh for free.
    """
    global _peak_mb
    raw = _resolve_reader()()
    if raw is None:
        return None
    value = raw / _MB
    with _peak_lock:
        if value > _peak_mb:
            _peak_mb = value
    return value


def peak_mb() -> float:
    """Highest RSS observed across all samples taken so far (0.0 if never sampled)."""
    with _peak_lock:
        return _peak_mb


def source() -> str:
    """Which backend is sampling RSS: psutil | procfs | windows | unavailable."""
    _resolve_reader()
    return _source


def limit_mb() -> Optional[float]:
    """
    The memory ceiling in MB: the configured value, else the cgroup limit.

    Returns ``None`` when neither is known — in which case callers must omit the
    percentage rather than compare against the host's total RAM, which on Render
    is far larger than the container is allowed to use.
    """
    if _configured_limit_mb:
        return _configured_limit_mb
    return _detect_cgroup_limit_mb()


def _detect_cgroup_limit_mb() -> Optional[float]:
    """Read the container memory ceiling (cgroup v2 first, then v1)."""
    for path in (
        "/sys/fs/cgroup/memory.max",                    # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    ):
        try:
            with open(path, "r", encoding="ascii") as handle:
                raw = handle.read().strip()
        except Exception:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # v1 reports a near-2**63 sentinel when unlimited; treat it as unknown.
        if value <= 0 or value >= 1 << 62:
            return None
        return value / _MB
    return None


def memory_status() -> dict:
    """
    Machine-readable memory snapshot, for ``GET /api/v1/health``.

    Keys are always present so the response shape does not change when sampling
    is unavailable; ``rss_mb`` is ``None`` in that case.
    """
    current = rss_mb()
    ceiling = limit_mb()
    pct = (
        round(current / ceiling * 100, 1)
        if current is not None and ceiling
        else None
    )
    return {
        "rss_mb": round(current, 1) if current is not None else None,
        "peak_mb": round(peak_mb(), 1) if peak_mb() else None,
        "limit_mb": round(ceiling, 1) if ceiling else None,
        "pct_of_limit": pct,
        "source": source(),
    }


# ──────────────────────────────────────────────────────────── rendering

def format_status(baseline_mb: Optional[float] = None, include_peak: bool = True) -> str:
    """
    Render a compact one-line RAM status, e.g.::

        rss=251.3MB (+101.2MB) peak=251.3MB 49.1% of 512MB

    Args:
        baseline_mb: If given, include the delta against it — this is what makes
            the MetricFlow engine's cost visible as a single number.
        include_peak: Append the process high-water mark.

    Returns:
        A string safe to embed in any log message. ``rss=n/a`` when unavailable.
    """
    current = rss_mb()
    if current is None:
        return "rss=n/a"

    parts = [f"rss={current:.1f}MB"]
    if baseline_mb is not None:
        parts.append(f"({current - baseline_mb:+.1f}MB)")
    if include_peak:
        parts.append(f"peak={peak_mb():.1f}MB")

    ceiling = limit_mb()
    if ceiling:
        parts.append(f"{current / ceiling * 100:.1f}% of {ceiling:.0f}MB")
    return " ".join(parts)


def log_status(
    label: str,
    target_logger: Optional[logging.Logger] = None,
    baseline_mb: Optional[float] = None,
) -> Optional[float]:
    """
    Log one RAM status line and return the sampled RSS.

    Emitted at WARNING while RSS sits above ``warn_pct`` of the limit, INFO
    otherwise. Flooding is not a concern because every caller of this function is
    low-frequency (startup, engine build, heartbeat) — the per-request line takes
    the cheaper :func:`format_status` path instead.

    Args:
        label: What the measurement is *about* ("startup", "MetricFlow engine
            built", "heartbeat") — this is the field you grep for.
        target_logger: Logger to emit on; defaults to this module's. Pass the
            caller's logger so the line is attributed to the right module.
        baseline_mb: Optional earlier RSS to show a delta against.

    Returns:
        The sampled RSS in MB, or ``None`` — convenient as the next call's
        ``baseline_mb``.
    """
    log = target_logger or logger
    current = rss_mb()
    rendered = format_status(baseline_mb=baseline_mb)

    ceiling = limit_mb()
    if current is not None and ceiling and (current / ceiling * 100) >= _warn_pct:
        log.warning(
            "RAM | %s | %s | above %.0f%% of the limit", label, rendered, _warn_pct
        )
    else:
        log.info("RAM | %s | %s", label, rendered)
    return current


# ──────────────────────────────────────────────────────────── heartbeat

def start_heartbeat(
    interval_seconds: int,
    target_logger: Optional[logging.Logger] = None,
    stop_event: Optional[threading.Event] = None,
) -> Optional[threading.Thread]:
    """
    Log a RAM status line every ``interval_seconds`` on a daemon thread.

    The per-request line only fires while traffic flows, so it cannot show a leak
    on an idle instance or the drift that precedes an OOM restart. The heartbeat
    covers that gap. It samples one integer, so the cost is irrelevant next to
    the interval.

    Args:
        interval_seconds: Seconds between samples. ``<= 0`` disables the
            heartbeat and returns ``None``.
        target_logger: Logger to emit on; defaults to this module's.
        stop_event: Set this to end the loop; otherwise the daemon thread simply
            dies with the process.

    Returns:
        The started thread, or ``None`` when disabled.
    """
    if interval_seconds <= 0:
        return None

    log = target_logger or logger
    event = stop_event or threading.Event()

    def _loop() -> None:
        while not event.wait(interval_seconds):
            try:
                log_status("heartbeat", target_logger=log)
            except Exception as exc:  # never let telemetry kill its own thread
                log.debug("Memory heartbeat sample failed: %s", exc)

    thread = threading.Thread(target=_loop, name="memory-heartbeat", daemon=True)
    thread.start()
    log.info(
        "✓ RAM heartbeat ENABLED — logging every %ds (backend: %s).",
        interval_seconds,
        source(),
    )
    return thread
