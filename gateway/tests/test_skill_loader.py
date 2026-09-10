"""
tests/test_skill_loader.py — Integration smoke tests for skill_loader.

These tests run against the real .md files on disk (no mocking).
If a skill file is moved or a section header is renamed, these tests
will break immediately and pinpoint exactly what changed.

Run from gateway/:
    python -m pytest tests/test_skill_loader.py -v
"""

import pytest

# A plain import now that the loader lives in gateway/core/, same as every other
# test in this directory. This was an importlib file-path bootstrap when the
# loader sat in a sibling `backend/` package the gateway could not import.
from core.skill_loader import get_skill_section, load_skill


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
