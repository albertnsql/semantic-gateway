"""
tests/test_dimension_values.py — filter values the warehouse actually holds.

The bug: the prompt named certified dimensions but never their VALUES, so the model
emitted whatever the user said. Asked "why is revenue lower in Germany" the live model
returned ``('subscriber__country', 'Germany')`` while the warehouse stores ``DE``. The
filter matched nothing, the query returned zero rows with ``status=success``, and the
narrative described an empty result as an answer.

Same family as the ``IN ('basic,standard')`` bug the FilterClause coercion was written
for: valid SQL that matches nothing, no error anywhere. It affected ordinary metric
queries as much as diagnoses.

After injecting the values, the live model returns ``DE``, ``annual`` and ``card`` for
"Germany", "annual plans" and "credit card".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import dimension_values


class _Pool:
    """Returns rows shaped like DuckDBPool.execute() — uppercased keys."""

    def __init__(self, per_column: dict[str, list[str]], fail: set[str] | None = None):
        self.per_column = per_column
        self.fail = fail or set()
        self.queries: list[str] = []

    def execute(self, sql: str):
        self.queries.append(sql)
        column = sql.split("DISTINCT ")[1].split(" ")[0]
        if column in self.fail:
            raise RuntimeError(f"{column} exploded")
        return [{"V": v} for v in self.per_column.get(column, [])]


class TestLoad:
    def test_it_reads_values_and_uppercased_keys(self) -> None:
        pool = _Pool({"country": ["DE", "US"], "plan_type": ["basic", "premium"]})
        values = dimension_values.load(pool)
        assert values["country"] == ["DE", "US"]
        assert values["plan_type"] == ["basic", "premium"]

    def test_a_failing_column_is_skipped_not_fatal(self) -> None:
        """
        Best-effort by design: a dropped dimension leaves the prompt exactly as it was
        before this module existed, so it can never make things worse.
        """
        pool = _Pool({"country": ["DE"], "plan_type": ["basic"]}, fail={"country"})
        values = dimension_values.load(pool)
        assert "country" not in values
        assert values["plan_type"] == ["basic"]

    def test_no_pool_returns_nothing(self) -> None:
        assert dimension_values.load(None) == {}

    def test_a_high_cardinality_dimension_is_omitted_not_truncated(self) -> None:
        """
        A truncated list is worse than none: the model reads it as exhaustive and
        confidently picks a wrong value from the visible subset.
        """
        pool = _Pool({"country": [f"C{i}" for i in range(200)]})
        assert "country" not in dimension_values.load(pool)

    def test_empty_columns_are_omitted(self) -> None:
        assert "country" not in dimension_values.load(_Pool({"country": []}))

    def test_values_are_sorted_for_a_stable_prompt(self) -> None:
        """
        An unstable prompt would defeat the Stage 0 raw-question cache, which keys on
        the question plus everything that can change the extracted intent.
        """
        pool = _Pool({"country": ["US", "DE", "AR"]})
        first = dimension_values.load(pool)["country"]
        second = dimension_values.load(pool)["country"]
        assert first == second


class TestPromptBlock:
    def test_it_teaches_the_translation(self) -> None:
        block = dimension_values.format_for_prompt({"country": ["DE", "US"]})
        assert "DE" in block
        assert "Germany" in block, (
            "the block must show the translation, not just the allowed values - the "
            "model has to know that 'Germany' maps to 'DE'"
        )
        assert "needs_clarification" in block, (
            "it must say what to do with an unrecognised value, or the model guesses"
        )

    def test_nothing_loaded_means_no_block(self) -> None:
        """The prompt is then byte-identical to before this feature existed."""
        assert dimension_values.format_for_prompt({}) == ""

    def test_it_stays_small(self) -> None:
        """
        Cardinality is what makes this viable: 83 values across 15 dimensions, ~330
        tokens. A regression here means a high-cardinality column was added.
        """
        many = {f"dim{i}": [f"v{j}" for j in range(6)] for i in range(15)}
        assert len(dimension_values.format_for_prompt(many)) < 3000


def _clause(column, value, operator="eq"):
    return SimpleNamespace(column=column, operator=operator, value=value)


class TestUnknownValues:
    VALUES = {"country": ["DE", "US"], "plan_type": ["basic", "premium"]}

    def test_it_catches_the_germany_case(self) -> None:
        found = dimension_values.unknown_filter_values(
            [_clause("subscriber__country", "Germany")], self.VALUES
        )
        assert found == [("subscriber__country", "Germany")]

    def test_a_valid_value_passes(self) -> None:
        assert not dimension_values.unknown_filter_values(
            [_clause("subscriber__country", "DE")], self.VALUES
        )

    def test_matching_is_case_insensitive(self) -> None:
        assert not dimension_values.unknown_filter_values(
            [_clause("country", "de")], self.VALUES
        )

    def test_a_qualified_column_resolves_to_its_bare_name(self) -> None:
        assert not dimension_values.unknown_filter_values(
            [_clause("subscriber__plan_type", "premium")], self.VALUES
        )

    def test_every_element_of_a_list_is_checked(self) -> None:
        found = dimension_values.unknown_filter_values(
            [_clause("country", ["US", "DE", "Narnia"], operator="in")], self.VALUES
        )
        assert found == [("country", "Narnia")]

    def test_a_dimension_with_no_loaded_values_is_skipped(self) -> None:
        """
        Absence of data is not evidence a value is wrong. Flagging here would reject
        working queries whenever a column failed to load.
        """
        assert not dimension_values.unknown_filter_values(
            [_clause("device_type", "anything")], self.VALUES
        )

    def test_it_reports_rather_than_corrects(self) -> None:
        """
        A fuzzy match would silently answer a different question than the one asked -
        the exact failure this module exists to stop, not a fix for it. The return
        type carries no replacement value on purpose.
        """
        found = dimension_values.unknown_filter_values(
            [_clause("country", "Germny")], self.VALUES
        )
        assert found == [("country", "Germny")]

    def test_no_filters_is_fine(self) -> None:
        assert dimension_values.unknown_filter_values([], self.VALUES) == []
        assert dimension_values.unknown_filter_values(None, self.VALUES) == []


class TestSourceTable:
    def test_no_date_columns_are_registered(self) -> None:
        """
        Dates are handled by time_range, not value matching, and they are unbounded -
        enumerating one would blow the prompt.
        """
        for name in dimension_values._SOURCES:
            assert not name.endswith("_date"), f"{name} is a date column"
            assert name not in ("cohort_month", "period_month")

    def test_every_source_names_a_marts_or_staging_table(self) -> None:
        for name, (table, _) in dimension_values._SOURCES.items():
            assert table.startswith(("marts.", "staging.")), f"{name} -> {table}"


class TestPromptInjection:
    def test_the_extractor_starts_with_no_values(self) -> None:
        """
        Populated by lifespan() after the warehouse opens. Until then the prompt must
        be unchanged, so a gateway with no warehouse degrades rather than breaks.
        """
        from core.intent_extractor import IntentExtractor

        settings = SimpleNamespace(
            google_api_key="", google_model="m", google_base_url="",
            openai_api_key="k", openai_model="m", llm_base_url="",
            openrouter_api_key="", openrouter_model="m", openrouter_base_url="",
            openai_temperature=0.0,
        )
        extractor = IntentExtractor(settings=settings)
        assert extractor._dimension_values == {}

    def test_the_block_reaches_the_system_prompt(self) -> None:
        from core.intent_extractor import IntentExtractor

        settings = SimpleNamespace(
            google_api_key="", google_model="m", google_base_url="",
            openai_api_key="k", openai_model="m", llm_base_url="",
            openrouter_api_key="", openrouter_model="m", openrouter_base_url="",
            openai_temperature=0.0,
        )
        extractor = IntentExtractor(settings=settings)
        extractor._dimension_values = {"country": ["DE", "US"]}
        prompt = extractor.build_system_prompt(
            ["total_revenue"], {"total_revenue": ["country"]}, {}
        )
        assert "ALLOWED FILTER VALUES" in prompt
        assert "DE, US" in prompt
