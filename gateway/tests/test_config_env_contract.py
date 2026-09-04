"""
tests/test_config_env_contract.py — settings fields that must keep reading a
specific, already-deployed environment variable.

`DISABLE_RAG` was read via `os.getenv("DISABLE_RAG", "false").lower() == "true"`
in main.py, against the repo convention that configuration lives in config.py.
Moving it to a settings field is safe ONLY because pydantic-settings has no
`env_prefix` here and `case_sensitive=False`, so a field named `disable_rag`
resolves the existing `DISABLE_RAG` variable.

That makes the FIELD NAME part of the deployment contract, which is the kind of
coupling nothing normally checks. A rename — or an inversion to something like
`rag_enabled` — would leave `DISABLE_RAG=true` in Render silently ignored and
turn retrieval back on in production, where the ChromaDB index does not even
exist. It would fail as a warning in a log, not as an error.
"""

from __future__ import annotations

import pytest

from config import Settings


@pytest.fixture(autouse=True)
def _no_env_file(monkeypatch, tmp_path):
    """
    Read the environment only.

    Without this the repo's gateway/.env leaks in and the test would assert
    against a developer's local values rather than the field/variable mapping.
    """
    monkeypatch.chdir(tmp_path)


def _settings(**overrides) -> Settings:
    """Construct Settings with the one genuinely required field supplied.

    `openai_api_key` (the GROQ key, despite the name) has no default, so hiding
    the .env file makes a bare `Settings()` raise. Supplying it keeps these tests
    about the DISABLE_RAG mapping and nothing else.
    """
    return Settings(openai_api_key="test-key", **overrides)


class TestDisableRag:
    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("True", True), ("TRUE", True), ("1", True),
        ("false", False), ("False", False), ("0", False),
    ])
    def test_the_deployed_variable_name_still_drives_the_field(
        self, monkeypatch, raw, expected
    ) -> None:
        monkeypatch.setenv("DISABLE_RAG", raw)
        assert _settings().disable_rag is expected

    def test_it_defaults_to_false_when_unset(self, monkeypatch) -> None:
        """
        Absent configuration must mean "behave as before" — RAG is attempted and
        then degrades on its own when the index is missing. Defaulting to True
        would disable a feature nobody asked to disable.
        """
        monkeypatch.delenv("DISABLE_RAG", raising=False)
        assert _settings().disable_rag is False

    def test_the_polarity_is_not_inverted(self, monkeypatch) -> None:
        """
        Guards the rename hazard directly: DISABLE_RAG=true must mean "RAG off".
        An inverted field would read the same variable and mean the opposite.
        """
        monkeypatch.setenv("DISABLE_RAG", "true")
        settings = _settings()
        assert settings.disable_rag, (
            "DISABLE_RAG=true must disable RAG — if this field was renamed or "
            "inverted, production would silently re-enable retrieval against an "
            "index that is never deployed"
        )

    def test_main_reads_the_setting_not_the_environment(self) -> None:
        """
        The point of the move. A stray `os.getenv("DISABLE_RAG")` would work but
        bypass the typed default and the single source of truth.
        """
        import pathlib

        source = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(
            encoding="utf-8"
        )
        assert 'os.getenv("DISABLE_RAG"' not in source
        assert "settings.disable_rag" in source
