"""
backend/core/skill_loader.py — Utility for loading skill markdown files.

Provides two pure functions for reading skill files from
backend/skills/{skill_name}.md and extracting individual sections by header.

No external dependencies beyond the Python standard library.
"""

from __future__ import annotations

import re
from pathlib import Path

# Resolve the skills directory relative to this file's location.
# This file lives at backend/core/skill_loader.py, so skills are one level up
# then into the skills/ subdirectory.
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def load_skill(skill_name: str) -> str:
    """
    Load the full markdown content of a named skill file.

    Args:
        skill_name: The base name of the skill (without the .md extension),
            e.g. ``"streaming_analytics"`` loads
            ``backend/skills/streaming_analytics.md``.

    Returns:
        The full markdown string contents of the skill file.

    Raises:
        FileNotFoundError: If the skill file does not exist, with a clear
            message showing the expected path.
    """
    skill_path = _SKILLS_DIR / f"{skill_name}.md"
    if not skill_path.exists():
        raise FileNotFoundError(
            f"Skill file not found: '{skill_path}'. "
            f"Expected a file at backend/skills/{skill_name}.md. "
            f"Available skills: {[p.stem for p in _SKILLS_DIR.glob('*.md')]}"
        )
    return skill_path.read_text(encoding="utf-8")


def get_skill_section(skill_name: str, section_header: str) -> str:
    """
    Extract the content of a specific section from a skill file.

    Locates the first heading (``##`` or ``###``) whose text matches
    *section_header* (case-insensitive), then returns all content up to
    the next heading of equal or higher level.

    Args:
        skill_name: The base name of the skill (same as :func:`load_skill`).
        section_header: The heading text to search for, e.g.
            ``"Table Reference"`` or ``"Gotchas"``.

    Returns:
        The section content as a string (excluding the heading line itself),
        stripped of leading/trailing whitespace.  Returns an empty string if
        the section is not found — does not raise.
    """
    try:
        content = load_skill(skill_name)
    except FileNotFoundError:
        return ""

    lines = content.splitlines()

    # Match lines that are Markdown headings (## or ###, any depth)
    heading_re = re.compile(r"^(#{1,6})\s+(.*)")

    target_level: int | None = None
    target_line: int | None = None

    # Find the target heading
    for i, line in enumerate(lines):
        m = heading_re.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            # Match if the heading text exactly equals the search string,
            # OR if it ends with " — <search>" (e.g. "Section 3 — Gotchas"
            # is matched by searching for "Gotchas").
            needle = section_header.lower()
            haystack = text.lower()
            if haystack == needle or haystack.endswith(f"\u2014 {needle}") or haystack.endswith(f"- {needle}"):
                target_level = level
                target_line = i
                break

    if target_line is None:
        return ""

    # Collect lines after the heading until we hit a heading of equal or higher level
    section_lines: list[str] = []
    for line in lines[target_line + 1 :]:
        m = heading_re.match(line)
        if m and len(m.group(1)) <= target_level:  # type: ignore[operator]
            break
        section_lines.append(line)

    return "\n".join(section_lines).strip()
