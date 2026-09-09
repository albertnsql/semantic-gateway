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


# ─────────────────────────────────────────────────────────────────────────────
# Cerebras became the PRIMARY rung on 2026-09-09.
#
# The env-var contract matters here for the same reason DISABLE_RAG does: with
# no `env_prefix` and `case_sensitive=False`, the FIELD NAME is what resolves
# `CEREBRAS_API_KEY` in Render. Rename the field and the key set in the
# dashboard is silently ignored — the rung is then skipped (no key), the chain
# quietly falls back to Google, and that is unreachable from Render.
# ─────────────────────────────────────────────────────────────────────────────
class TestCerebrasIsThePrimaryRung:
    def test_default_order_is_cerebras_google_groq(self):
        assert _settings().llm_provider_order == "cerebras,google,groq"

    def test_openrouter_is_not_in_the_default_chain(self):
        # It holds no credit; an unfunded rung returns a fast 402 and consumes a
        # slot, which is worse than being absent.
        s = _settings(cerebras_api_key="csk-x", google_api_key="g",
                      openrouter_api_key="or-x")
        assert [label for label, *_ in s.provider_chain()] == [
            "cerebras", "google", "groq",
        ]

    def test_cerebras_resolves_to_qwen_on_the_openai_compatible_endpoint(self):
        s = _settings(cerebras_api_key="csk-x")
        label, key, base_url, model = s.provider_chain()[0]
        assert label == "cerebras"
        assert key == "csk-x"
        assert base_url == "https://api.cerebras.ai/v1"
        assert model == "qwen-3.8-27b"

    def test_the_api_key_field_reads_the_CEREBRAS_API_KEY_variable(self, monkeypatch):
        monkeypatch.setenv("CEREBRAS_API_KEY", "csk-from-env")
        assert _settings().cerebras_api_key == "csk-from-env"

    def test_the_model_field_reads_the_CEREBRAS_MODEL_variable(self, monkeypatch):
        monkeypatch.setenv("CEREBRAS_MODEL", "qwen-3.8-27b-override")
        assert _settings().cerebras_model == "qwen-3.8-27b-override"

    def test_an_unconfigured_cerebras_key_is_skipped_not_offered_keyless(self):
        # A rung with no key must drop out entirely rather than be handed to the
        # SDK with api_key="" — that turns a config gap into a 401 at query time.
        s = _settings(google_api_key="g")
        assert [label for label, *_ in s.provider_chain()] == ["google", "groq"]

    def test_the_chain_is_ordered_by_the_setting_not_by_the_dict(self):
        s = _settings(cerebras_api_key="csk-x", google_api_key="g",
                      llm_provider_order="groq,cerebras,google")
        assert [label for label, *_ in s.provider_chain()] == [
            "groq", "cerebras", "google",
        ]


# ─────────────────────────────────────────────────────────────────────────────
# qwen-3.8-27b is a REASONING model and its thinking tokens count against
# max_tokens without appearing in the response or in completion_tokens_details.
# Measured 2026-09-09 on the real ~9,800-token intent prompt: at max_tokens=300
# one question returned finish_reason=length, completion=300 and content="" —
# and the narrative call at max_tokens=150 did the same. Empty content, not
# merely truncated, so the intent path raised and the prose silently vanished.
#
# reasoning_effort="none" fixed both AND was faster (that narrative went from
# 3.18s/150-tokens/empty to 0.32s/55-tokens/complete).
#
# It is per-provider because providers validate unknown parameters — Cerebras
# itself 400s on `chat_template_kwargs`.
# ─────────────────────────────────────────────────────────────────────────────
class TestPerProviderRequestKwargs:
    def test_cerebras_gets_reasoning_suppressed(self):
        assert _settings().llm_request_kwargs("cerebras") == {
            "reasoning_effort": "none"
        }

    @pytest.mark.parametrize("label", ["google", "groq", "openrouter", "unknown"])
    def test_every_other_provider_gets_nothing(self, label):
        # Must be an empty dict, not None — call sites splat it unconditionally.
        assert _settings().llm_request_kwargs(label) == {}

    def test_the_effort_level_is_configurable(self, monkeypatch):
        monkeypatch.setenv("CEREBRAS_REASONING_EFFORT", "low")
        assert _settings().llm_request_kwargs("cerebras") == {
            "reasoning_effort": "low"
        }

    def test_an_empty_effort_stops_sending_the_parameter(self):
        s = _settings(cerebras_reasoning_effort="")
        assert s.llm_request_kwargs("cerebras") == {}

    def test_it_works_unbound_against_a_duck_typed_settings(self):
        # Same requirement provider_chain() has: test doubles are SimpleNamespace
        # or MagicMock and rarely carry every field.
        from types import SimpleNamespace

        fake = SimpleNamespace(cerebras_reasoning_effort="none")
        assert Settings.llm_request_kwargs(fake, "cerebras") == {
            "reasoning_effort": "none"
        }
        assert Settings.llm_request_kwargs(SimpleNamespace(), "google") == {}
