"""
backend/tests/test_skill_loader.py — Integration smoke tests for skill_loader.

These tests run against the real .md files on disk (no mocking).
If a skill file is moved or a section header is renamed, these tests
will break immediately and pinpoint exactly what changed.

Run from the project root:
    python -m pytest backend/tests/test_skill_loader.py -v
"""

import importlib.util
import pathlib
import pytest

# ---------------------------------------------------------------------------
# Bootstrap: load skill_loader directly from its file path so this test file
# works regardless of how pytest is invoked (project root, gateway/, etc.)
# ---------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).resolve().parent          # backend/tests/
_SKILL_LOADER_PATH = _HERE.parent / "core" / "skill_loader.py"

_spec = importlib.util.spec_from_file_location("skill_loader", _SKILL_LOADER_PATH)
_mod = importlib.util.module_from_spec(_spec)            # type: ignore[arg-type]
_spec.loader.exec_module(_mod)                           # type: ignore[union-attr]

load_skill = _mod.load_skill
get_skill_section = _mod.get_skill_section


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_load_skill_returns_string():
    """load_skill() returns a non-empty string containing the expected section."""
    result = load_skill("streaming_analytics")
    assert isinstance(result, str), "Expected load_skill to return a str"
    assert len(result) > 0, "Expected a non-empty string from load_skill"
    assert "Semantic Layer" in result, (
        "Expected 'Semantic Layer' to appear in streaming_analytics.md"
    )


def test_load_skill_missing_raises():
    """load_skill() raises FileNotFoundError with a descriptive message for unknown skills."""
    with pytest.raises(FileNotFoundError) as exc_info:
        load_skill("nonexistent_skill")
    assert "nonexistent_skill" in str(exc_info.value), (
        "FileNotFoundError message should contain the missing skill name"
    )


def test_get_skill_section_returns_content():
    """get_skill_section() extracts the Gotchas section and it contains expected content."""
    result = get_skill_section("streaming_analytics", "Gotchas")
    assert isinstance(result, str), "Expected get_skill_section to return a str"
    assert len(result) > 0, "Expected a non-empty string for the Gotchas section"
    assert "fan-out" in result.lower(), (
        "Expected 'fan-out' (case-insensitive) to appear in the Gotchas section"
    )


def test_get_skill_section_missing_returns_empty():
    """get_skill_section() returns '' for a header that does not exist — does not raise."""
    result = get_skill_section("streaming_analytics", "Nonexistent Header")
    assert result == "", (
        f"Expected empty string for missing section, got: {result!r}"
    )
