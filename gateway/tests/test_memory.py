"""
tests/test_memory.py — RSS sampling and RAM log rendering (core/memory.py).

The contract worth pinning is not the numbers (they vary per host) but the
failure behaviour: memory telemetry sits in the request path and in lifespan, so
it must degrade to "n/a" rather than raise, and it must never invent a percentage
against a limit it does not actually know.
"""

from __future__ import annotations

import logging
import threading

import pytest

from core import memory


@pytest.fixture(autouse=True)
def _clean_memory_module():
    """Each test starts with no resolved backend, no peak and no configured limit."""
    memory.reset_for_tests()
    yield
    memory.reset_for_tests()


# ────────────────────────────────────────────────────────────── sampling

def test_rss_is_a_plausible_positive_number() -> None:
    """At least one backend must work on any platform the suite runs on."""
    value = memory.rss_mb()
    assert value is not None, f"no RSS backend worked (source={memory.source()})"
    # A Python process running pytest is comfortably inside this window; the point
    # is to catch a unit error (bytes reported as MB, or pages as bytes).
    assert 5.0 < value < 100_000.0


def test_source_names_the_backend_actually_used() -> None:
    assert memory.source() in {"psutil", "procfs", "windows", "unavailable"}


def test_peak_tracks_the_highest_sample_and_never_decreases() -> None:
    memory.rss_mb()
    first_peak = memory.peak_mb()
    assert first_peak > 0

    for _ in range(3):
        memory.rss_mb()
    assert memory.peak_mb() >= first_peak


def test_peak_is_zero_before_any_sample() -> None:
    assert memory.peak_mb() == 0.0


# ─────────────────────────────────────────────── degradation when unavailable

def test_rss_returns_none_when_every_backend_fails(monkeypatch) -> None:
    monkeypatch.setattr(memory, "_BACKENDS", (("stub", lambda: None),))
    memory.reset_for_tests()

    assert memory.rss_mb() is None
    assert memory.source() == "unavailable"


def test_format_status_degrades_to_na_rather_than_raising(monkeypatch) -> None:
    monkeypatch.setattr(memory, "_BACKENDS", (("stub", lambda: None),))
    memory.reset_for_tests()

    assert memory.format_status() == "rss=n/a"
    assert memory.format_status(baseline_mb=100.0) == "rss=n/a"


def test_log_status_survives_an_unavailable_backend(monkeypatch, caplog) -> None:
    """A dead sampler must not be able to break a request log line."""
    monkeypatch.setattr(memory, "_BACKENDS", (("stub", lambda: None),))
    memory.reset_for_tests()

    with caplog.at_level(logging.INFO):
        assert memory.log_status("startup") is None
    assert "rss=n/a" in caplog.text


def test_a_raising_backend_is_treated_as_unavailable(monkeypatch) -> None:
    """Resolution must swallow a backend that raises, not propagate into a request."""
    def _explode() -> int:
        raise OSError("no /proc on this host")

    monkeypatch.setattr(memory, "_BACKENDS", (("boom", _explode),))
    memory.reset_for_tests()

    assert memory.rss_mb() is None
    assert memory.source() == "unavailable"


def test_a_backend_that_fails_after_resolution_returns_none(monkeypatch) -> None:
    """A backend can work at startup and break later (a container losing /proc)."""
    state = {"calls": 0}

    def _flaky() -> int:
        # Call 1 is the resolution probe, call 2 the first real read, then it breaks.
        state["calls"] += 1
        if state["calls"] > 2:
            raise OSError("gone")
        return 251 * 1024 * 1024

    monkeypatch.setattr(memory, "_BACKENDS", (("flaky", _flaky),))
    memory.reset_for_tests()

    assert memory.rss_mb() == pytest.approx(251.0)
    assert memory.rss_mb() is None            # now broken, still no exception
    assert memory.format_status() == "rss=n/a"


# ────────────────────────────────────────────────────────────── rendering

def _stub_rss(monkeypatch, mb: float) -> None:
    monkeypatch.setattr(memory, "_BACKENDS", (("stub", lambda: int(mb * 1024 * 1024)),))
    memory.reset_for_tests()


def test_format_status_includes_rss_peak_and_percentage(monkeypatch) -> None:
    _stub_rss(monkeypatch, 251.0)
    memory.configure(limit_mb=512)

    rendered = memory.format_status()

    assert "rss=251.0MB" in rendered
    assert "peak=251.0MB" in rendered
    assert "49.0% of 512MB" in rendered


def test_format_status_renders_a_signed_delta(monkeypatch) -> None:
    """The +101 MB engine cost is the whole reason the delta exists."""
    _stub_rss(monkeypatch, 251.0)

    assert "(+101.0MB)" in memory.format_status(baseline_mb=150.0)
    assert "(-49.0MB)" in memory.format_status(baseline_mb=300.0)


def test_no_percentage_is_invented_when_the_limit_is_unknown(monkeypatch) -> None:
    """Guessing the host's total RAM would make the figure meaningless on Render."""
    _stub_rss(monkeypatch, 251.0)
    monkeypatch.setattr(memory, "_detect_cgroup_limit_mb", lambda: None)
    memory.configure(limit_mb=0)  # 0 = auto-detect, and detection just failed

    rendered = memory.format_status()

    assert "rss=251.0MB" in rendered
    assert "%" not in rendered
    assert memory.limit_mb() is None


def test_configured_limit_overrides_cgroup_detection(monkeypatch) -> None:
    monkeypatch.setattr(memory, "_detect_cgroup_limit_mb", lambda: 2048.0)
    memory.configure(limit_mb=512)

    assert memory.limit_mb() == 512.0


def test_cgroup_limit_is_used_when_nothing_is_configured(monkeypatch) -> None:
    monkeypatch.setattr(memory, "_detect_cgroup_limit_mb", lambda: 512.0)
    memory.configure(limit_mb=0)

    assert memory.limit_mb() == 512.0


# ────────────────────────────────────────────────────────── warn threshold

def test_status_logs_at_warning_above_the_threshold(monkeypatch, caplog) -> None:
    _stub_rss(monkeypatch, 470.0)  # 91.8% of 512
    memory.configure(limit_mb=512, warn_pct=85.0)

    with caplog.at_level(logging.INFO):
        memory.log_status("heartbeat")

    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "above 85% of the limit" in caplog.text


def test_status_logs_at_info_below_the_threshold(monkeypatch, caplog) -> None:
    _stub_rss(monkeypatch, 251.0)  # 49% of 512
    memory.configure(limit_mb=512, warn_pct=85.0)

    with caplog.at_level(logging.INFO):
        memory.log_status("heartbeat")

    assert [r.levelno for r in caplog.records] == [logging.INFO]


def test_log_status_returns_the_sample_for_use_as_the_next_baseline(monkeypatch) -> None:
    _stub_rss(monkeypatch, 150.0)
    assert memory.log_status("startup") == pytest.approx(150.0)


def test_label_appears_in_the_log_line(monkeypatch, caplog) -> None:
    """The label is the grep key, so it has to survive into the message."""
    _stub_rss(monkeypatch, 251.0)

    with caplog.at_level(logging.INFO):
        memory.log_status("MetricFlow engine built in 19.4s (ok)")

    assert "RAM | MetricFlow engine built in 19.4s (ok)" in caplog.text


# ──────────────────────────────────────────────────────────── health payload

def test_memory_status_payload_shape_is_stable(monkeypatch) -> None:
    _stub_rss(monkeypatch, 251.0)
    memory.configure(limit_mb=512)

    status = memory.memory_status()

    assert status == {
        "rss_mb": 251.0,
        "peak_mb": 251.0,
        "limit_mb": 512.0,
        "pct_of_limit": 49.0,
        "source": "stub",
    }


def test_memory_status_keys_persist_when_sampling_is_unavailable(monkeypatch) -> None:
    """/health must not change shape just because RSS is unreadable."""
    monkeypatch.setattr(memory, "_BACKENDS", (("stub", lambda: None),))
    memory.reset_for_tests()

    status = memory.memory_status()

    assert set(status) == {"rss_mb", "peak_mb", "limit_mb", "pct_of_limit", "source"}
    assert status["rss_mb"] is None
    assert status["pct_of_limit"] is None


# ────────────────────────────────────────────────────────────── heartbeat

def test_heartbeat_is_disabled_by_a_non_positive_interval() -> None:
    assert memory.start_heartbeat(0) is None
    assert memory.start_heartbeat(-1) is None


def test_heartbeat_logs_and_stops_on_the_event(monkeypatch, caplog) -> None:
    _stub_rss(monkeypatch, 251.0)
    stop = threading.Event()

    # wait() returning False once, then True, gives exactly one sample and exit —
    # no sleeping, no flakiness.
    calls = {"n": 0}

    def _fake_wait(_timeout=None) -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(stop, "wait", _fake_wait)

    with caplog.at_level(logging.INFO):
        thread = memory.start_heartbeat(300, stop_event=stop)
        assert thread is not None
        thread.join(timeout=5)

    assert not thread.is_alive(), "heartbeat thread did not exit on the stop event"
    assert "RAM | heartbeat" in caplog.text


def test_heartbeat_thread_is_a_daemon(monkeypatch) -> None:
    """A non-daemon telemetry thread would hang shutdown."""
    _stub_rss(monkeypatch, 251.0)
    stop = threading.Event()
    thread = memory.start_heartbeat(3600, stop_event=stop)
    try:
        assert thread is not None and thread.daemon
    finally:
        stop.set()


def test_heartbeat_survives_a_sampling_failure(monkeypatch, caplog) -> None:
    """One bad sample must not kill the thread and silence RAM logging for good."""
    stop = threading.Event()

    def _boom(*_a, **_k):
        raise RuntimeError("sampler exploded")

    monkeypatch.setattr(memory, "log_status", _boom)

    calls = {"n": 0}

    def _fake_wait(_timeout=None) -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    monkeypatch.setattr(stop, "wait", _fake_wait)

    thread = memory.start_heartbeat(300, stop_event=stop)
    assert thread is not None
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert calls["n"] == 3, "loop stopped early — an exception escaped the sampler"
