"""
core/sql_generator.py — MetricFlow-governed SQL generation.

Single responsibility: translate a validated QueryIntent into a MetricFlow
CLI command, execute it with --explain to get governed SQL, and optionally
execute that SQL against Snowflake.

NEVER generates raw SQL directly — all SQL comes from MetricFlow's
semantic layer compilation.

SQL Template Cache:
    After MetricFlow compiles SQL the first time for a given metric+dimension
    combination, the result is stored in SQLTemplateCache (keyed without the
    time range).  Subsequent queries with the same metric+dimensions but a
    *different* time range skip the MetricFlow subprocess entirely (~44 s saved)
    and instead retrieve the cached template + inject the new date literals.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import concurrent.futures
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import snowflake.connector
from openai import OpenAI


# ──────────────────────────────────────────────── Input validation helpers

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Shell metacharacters that could allow command injection.
_SHELL_META_RE = re.compile(r"[;|&$`(){}\\<>!\"]")  # double-quote included


def _validate_date(value: str, label: str = "date") -> str:
    """Validate that *value* looks like ``YYYY-MM-DD``.

    Raises ``ValueError`` with a descriptive message when the check fails.
    Returns the original value unchanged when valid.
    """
    if not _DATE_RE.fullmatch(value):
        raise ValueError(
            f"Invalid {label}: {value!r} — expected YYYY-MM-DD format."
        )
    return value


def _sanitize_filter_value(value: str) -> str:
    """Reject filter values that contain shell metacharacters.

    This is a defence-in-depth check — with ``shell=False`` these characters
    are harmless, but we reject them anyway to surface bad LLM output early.
    """
    if _SHELL_META_RE.search(value):
        raise ValueError(
            f"Filter value contains disallowed characters: {value!r}"
        )
    return value


_OUTER_OP_MAP: dict[str, str] = {
    "eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
}


def _sql_literal(value) -> str:
    """Render a filter value as a SQL literal, quoting and escaping non-numerics."""
    raw = str(value)
    try:
        float(raw)
        return raw
    except ValueError:
        return "'{}'".format(raw.replace("'", "''"))


def _has_top_level_select(sql: str) -> bool:
    """
    True if *sql* contains a SELECT outside every parenthesised group.

    Used to detect truncated MetricFlow output. ``WITH cte AS ( SELECT … )`` with
    nothing after it is parenthesis-balanced and looks plausible, but it is not a
    runnable statement — its only SELECT lives inside the CTE. Scanning depth-0
    text distinguishes that from a real ``WITH … SELECT …`` or a plain SELECT.
    """
    depth = 0
    top_level: list[str] = []
    for ch in sql:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            top_level.append(ch)
    return re.search(r"\bSELECT\b", "".join(top_level), re.IGNORECASE) is not None


def wrap_with_outer_predicates(sql: str, post_filters: list) -> str:
    """
    Apply predicates to an already-compiled query by wrapping it in a subquery.

    Used when the filtered column is also a group-by dimension: the compiled SQL
    already SELECTs that column, so filtering the outer result is equivalent to
    compiling the predicate in — and it works for ratio metrics too, where
    re-aggregating a filtered subset would be wrong.

    Args:
        sql: Compiled SQL (dates already injected).
        post_filters: ``[(column, FilterClause), …]`` — columns must already be
            resolved to the names the compiled SQL emits.

    Returns:
        ``SELECT * FROM (<sql>) subq_flt WHERE …``, or *sql* unchanged if no
        predicate could be safely rendered.
    """
    if not post_filters:
        return sql

    predicates: list[str] = []
    for col, f in post_filters:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col):
            logger.warning("Outer predicate: skipping unsafe column name %r.", col)
            continue
        if f.operator == "in":
            vals = f.value if isinstance(f.value, list) else [f.value]
            if not vals:
                continue
            predicates.append(
                "{} IN ({})".format(col, ", ".join(_sql_literal(v) for v in vals))
            )
        else:
            op = _OUTER_OP_MAP.get(f.operator)
            if op is None:
                logger.warning(
                    "Outer predicate: unsupported operator %r on %r — skipping.",
                    f.operator, col,
                )
                continue
            predicates.append(f"{col} {op} {_sql_literal(f.value)}")

    if not predicates:
        return sql

    inner = sql.strip().rstrip(";")
    return (
        "SELECT * FROM (\n"
        + inner
        + "\n) subq_flt\nWHERE "
        + "\n  AND ".join(predicates)
    )
from pydantic import BaseModel

from core.exceptions import SnowflakeConnectionError, SQLGenerationError
from core.sql_template_cache import (
    SQLTemplateCache,
    parameterize_sql_dates,
    parameterize_by_auto_extraction,
    apply_grain_rounding,
    restore_sql_dates,
)

import importlib.util
# A plain import now that the loader lives alongside this module. This was the
# SECOND copy of a file-path importlib bootstrap (intent_extractor.py had the
# other), both walking three parents up into the old sibling `backend/` package.
# Worth knowing how it failed: both degrade with a warning and a disabled flag, so
# moving the loader without fixing this would have silently switched SQL review
# off -- and the reviewer only runs on the MetricFlow-failure path, so no test
# would have caught it.
try:
    from core.skill_loader import load_skill
    _SKILL_LOADER_AVAILABLE = True
except ImportError as _exc:  # pragma: no cover - defensive
    _SKILL_LOADER_AVAILABLE = False
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "core/skill_loader.py could not be imported (%s) — SQL review disabled.",
        _exc,
    )

if TYPE_CHECKING:
    from core.intent_extractor import QueryIntent, TimeRange
    from core.semantic_validator import ValidationResult
    from config import Settings

logger = logging.getLogger(__name__)

_QUERY_TIMEOUT_SECONDS = 30

_DYNAMIC_DIMENSION_MAP: dict[str, dict[str, str]] | None = None

# metric -> the entity names reachable from that metric's semantic models (primary
# + foreign). Used to tell a CORRECT entity prefix from a wrong one, which the
# bare-name check in the validator cannot do: `_get_bare_dimension()` strips
# `subscription__plan_type` down to `plan_type`, which IS certified for
# total_subscribers, so the query passes validation and then fails to resolve.
_DYNAMIC_METRIC_ENTITIES: dict[str, set[str]] | None = None

# metric -> the time granularity its offset window is expressed in ('month' for
# net_mrr_growth). MetricFlow refuses to resolve an offset metric unless
# metric_time is in the group-by, because there is nothing to offset along:
#   "specifies a time offset in input metrics ... However, group-by-items do not
#    include 'metric_time'."
# net_mrr_growth is the only such metric today.
_DYNAMIC_OFFSET_GRAINS: dict[str, str] | None = None

# Prefixes that are not entities and must never be "corrected". metric_time is
# MetricFlow's synthetic time dimension, required in the group-by for any metric
# with an offset window.
_RESERVED_DIM_PREFIXES: frozenset[str] = frozenset({"metric_time"})

# SNAPSHOT metrics: a count of the population AT A POINT IN TIME, not a sum over a
# period. fct_mrr_monthly holds one row per subscriber per month, so
# active_subscribers_count is meaningful within a month and meaningless across
# months -- COUNT(DISTINCT ...) over every period returns everyone who was EVER
# active.
#
# "Show me total subscribers by plan type" carries no time range, and answered
# 16,460 (the all-time union, which is also every row in dim_subscribers) when the
# real July 2026 base was 12,109. No error, no warning: a 36% overstatement
# reported as success.
#
# The semantic-layer fix for this is `non_additive_dimension: {name: period_month,
# window_choice: max}` on the measure. It was tried and REVERTED, because
# fct_mrr_monthly's spine is driven by current_date() and its churn rows carry a
# +1 month offset, so the newest period (2026-09) holds 531 rows with ZERO active
# subscribers. window_choice picks that phantom month and every answer becomes 0.
# Removing the phantom period means changing the fact table, which would move every
# MRR, churn and retention number in the app.
#
# So the default is applied here instead: no time range on a snapshot metric means
# "as of the latest period that actually has data".
#
# Every metric here is documented as MONTHLY and reads fct_mrr_monthly. Measured
# with no time range against the real warehouse:
#
#   metric            all periods   Aug 2026   true monthly
#   churn_rate           46.2%        4.4%      3.4 - 4.4%
#   retention_rate       53.8%       95.6%        ~96%
#   total_subscribers    16,460      11,774       11,774
#
# The ratios are worse than the count because numerator AND denominator both
# union: 46% is the share of subscribers who EVER churned, presented as a monthly
# rate on a business churning about 4% a month.
#
# `ltv` is deliberately NOT here even though it shares the
# active_subscribers_count measure. It is "total revenue per subscriber lifetime",
# so an all-period denominator is the correct one — defaulting it to the latest
# month would silently convert LTV into ARPU.
#
# The two internal building blocks (monthly_churned_subscribers,
# monthly_subscriber_base) are omitted too: they are hidden from the LLM by
# _INTERNAL_METRICS and never arrive as intent.metrics[0], so the default already
# applies through churn_rate / retention_rate.
_SNAPSHOT_METRICS: frozenset[str] = frozenset({
    "total_subscribers",
    "churn_rate",
    "retention_rate",
})


# Substrings that mark a MetricFlow QUERY-RESOLUTION failure rather than an
# engine problem. The distinction matters because the fallback chain exists for
# engine *unavailability*, where retrying through the CLI can genuinely succeed.
# A resolution error is deterministic: same manifest, same resolver, same input.
# Retrying it buys nothing and costs a subprocess.
#
# Observed in production 2026-08-21: net_mrr_growth failed to resolve in the warm
# engine, was retried through the CLI, returned the byte-identical error 24
# seconds later, then hit the governed builder and 500'd. Total 33.8 seconds to
# reach a conclusion available in the first 5 milliseconds.
_DETERMINISTIC_MF_ERRORS: tuple[str, ...] = (
    "got error(s) during query resolution",
    "does not match any of the available group-by-items",
    "specifies a time offset in input metrics",
    "unable to satisfy the query",
)


def is_deterministic_mf_error(exc: BaseException | str) -> bool:
    """
    True when MetricFlow rejected the QUERY, not when the engine misbehaved.

    Conservative by design: anything unrecognised is treated as retryable, so a
    genuine engine fault still gets its second chance through the subprocess.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _DETERMINISTIC_MF_ERRORS)


def _bare_dimension_name(dimension: str) -> str:
    """
    Convert MetricFlow-prefixed dimensions to the registry/warehouse column name.

    Examples:
    - subscriber__plan_type -> plan_type
    - subscription__period_month__month -> period_month
    """
    parts = dimension.split("__")
    if len(parts) >= 3:
        return "__".join(parts[1:-1])
    if len(parts) == 2:
        return parts[1]
    return dimension

def build_dimension_prefix_map() -> dict[str, dict[str, str]]:
    global _DYNAMIC_DIMENSION_MAP
    if _DYNAMIC_DIMENSION_MAP is not None:
        return _DYNAMIC_DIMENSION_MAP

    from config import settings
    import pathlib
    import json

    manifest_path = pathlib.Path(settings.manifest_path).parent / "semantic_manifest.json"
    
    if not manifest_path.exists():
        logger.warning("semantic_manifest.json not found at %s. Returning empty dimension map.", manifest_path)
        _DYNAMIC_DIMENSION_MAP = {}
        return _DYNAMIC_DIMENSION_MAP

    with open(manifest_path, 'r') as f:
        manifest = json.load(f)
        
    measure_to_sm = {}
    for sm in manifest.get('semantic_models', []):
        for measure in sm.get('measures', []):
            measure_to_sm[measure['name']] = sm
            
    global_dims = {}
    for sm in manifest.get('semantic_models', []):
        # PRIMARY entity only. `<entity>__<dim>` means "join to the semantic model
        # whose PRIMARY entity is <entity>, then read <dim> from it" — so a
        # dimension may only be prefixed with the entity of the model that OWNS
        # it. Including foreign entities here minted names for columns the target
        # model does not have: sem_mrr owns `billing_cycle` and declares
        # `subscriber` as a foreign key, which produced `subscriber__billing_cycle`
        # — dim_subscribers has no billing_cycle, so it can never resolve. It was
        # then chosen for total_subscribers and failed at query resolution.
        #
        # Legitimate duplicates survive: `country` is defined on BOTH
        # dim_subscribers and fct_stream_sessions, so `subscriber__country` and
        # `session__country` both remain candidates and the tie-break below picks.
        entities = [e['name'] for e in sm.get('entities', []) if e.get('type') == 'primary']
        if not entities:
            # No primary entity declared — fall back to the old behaviour rather
            # than silently dropping every dimension on this model.
            entities = [e['name'] for e in sm.get('entities', []) if e.get('type') == 'foreign']
        for dim in sm.get('dimensions', []):
            dim_name = dim['name']
            is_time = dim.get('type') == 'time'
            granularity = dim['type_params'].get('time_granularity') if is_time and dim.get('type_params') else None
            
            for entity in entities:
                prefixed = f"{entity}__{dim_name}__{granularity}" if is_time and granularity else f"{entity}__{dim_name}"
                if dim_name not in global_dims:
                    global_dims[dim_name] = []
                if prefixed not in global_dims[dim_name]:
                    global_dims[dim_name].append(prefixed)
                    
    metric_map = {}
    entity_map: dict[str, set[str]] = {}
    offset_grains: dict[str, str] = {}
    for metric in manifest.get('metrics', []):
        m_name = metric['name']
        input_measures = metric.get('type_params', {}).get('input_measures', [])
        used_sms = []
        for im in input_measures:
            sm = measure_to_sm.get(im['name'])
            if sm and sm not in used_sms:
                used_sms.append(sm)
                
        primary_entities = []
        model_entities = []   # ALL entities (primary + foreign) on the metric's models
        for sm in used_sms:
            for e in sm.get('entities', []):
                if e.get('type') == 'primary':
                    primary_entities.append(e['name'])
                model_entities.append(e['name'])
                    
        entity_map[m_name] = set(model_entities)

        # Offset window, if any. Recorded per metric so the group-by can be
        # completed before compiling instead of failing and falling back.
        for _inp in (metric.get('type_params') or {}).get('metrics') or []:
            _win = _inp.get('offset_window')
            if _win and _win.get('granularity'):
                offset_grains[m_name] = str(_win['granularity']).lower()
                break

        m_dim_map = {}
        reachable_entities = entity_map[m_name]
        for dim_name, prefixes in global_dims.items():
            # ── Reachability BEFORE any preference ────────────────────────────
            # A prefix is usable only when its entity is primary-or-foreign on one
            # of this metric's own semantic models, because that is what gives
            # MetricFlow a join path. Filtering first is the load-bearing part:
            # the hand-tuned preferences below used to run against the full
            # candidate list, so a preference could select an unreachable prefix
            # and nothing downstream could recover. recommendation_ctr lives on
            # sem_recommendation_events (event / subscriber / content) and the
            # 'session__ first' rule handed it `session__country` — 8 of its 11
            # certified dimensions failed at query resolution because of it.
            #
            # correct_dimension_entity() cannot clean this up afterwards: it looks
            # up its replacement in THIS map, finds the same wrong string, and
            # passes it through with a warning.
            candidates = [
                p for p in prefixes if p.split('__', 1)[0] in reachable_entities
            ]

            if not candidates:
                # No entry rather than an arbitrary prefixes[0]. Consumers fall
                # back to the bare name, which at least does not teach the LLM a
                # prefix that cannot compile.
                continue

            if len(candidates) == 1:
                m_dim_map[dim_name] = candidates[0]
                continue

            # ── Preferences, applied only among REACHABLE candidates ──────────
            # Every branch here encodes a deliberate choice between two prefixes
            # that both resolve; they are unchanged, and warmup_matrix pins
            # several of them (total_subscribers → subscriber__plan_type,
            # ltv → subscriber__*). Do not "simplify" these into the generic
            # primary-entity rule below — that would silently move numbers.
            chosen = None
            if m_name in ['total_subscribers', 'churned_subscribers']:
                for p in candidates:
                    if p.startswith('subscriber__'):
                        chosen = p
                        break
            elif m_name in ['churn_rate', 'retention_rate']:
                # churn_rate/retention_rate now live on fct_mrr_monthly:
                # prefer native subscription__ dims, fall back to subscriber__ joins.
                for p in candidates:
                    if p.startswith('subscription__'):
                        chosen = p
                        break
                if not chosen:
                    for p in candidates:
                        if p.startswith('subscriber__'):
                            chosen = p
                            break
            elif m_name == 'ltv':
                # ltv spans fct_payments (payment entity) AND dim_subscribers.
                # Prefer payment__ prefix for payment-domain dims, subscriber__ for subscriber dims.
                for p in candidates:
                    if p.startswith('payment__'):
                        chosen = p
                        break
                if not chosen:
                    for p in candidates:
                        if p.startswith('subscriber__'):
                            chosen = p
                            break
            elif m_name in ['mrr', 'expansion_mrr']:
                for p in candidates:
                    if p.startswith('subscription__'):
                        chosen = p
                        break
            elif m_name in ['engagement_rate', 'recommendation_ctr']:
                # engagement_rate: session__ dims (device_type) take priority;
                # subscriber__ dims (plan_type, country) are also valid via join.
                # For recommendation_ctr `session__` is now filtered out above as
                # unreachable, so this correctly falls through to subscriber__.
                for p in candidates:
                    if p.startswith('session__') or p.startswith('event__'):
                        chosen = p
                        break
                if not chosen:
                    for p in candidates:
                        if p.startswith('subscriber__'):
                            chosen = p
                            break

            if not chosen:
                for p in candidates:
                    if any(p.startswith(pe + '__') for pe in primary_entities):
                        chosen = p
                        break

            if not chosen:
                # Every remaining candidate is reachable by construction, so this
                # is a real choice between join paths rather than a shot in the dark.
                chosen = candidates[0]

            m_dim_map[dim_name] = chosen
                
        # Add common LLM abbreviation aliases
        if 'content_primary_genre' in m_dim_map:
            m_dim_map['primary_genre'] = m_dim_map['content_primary_genre']
            
        metric_map[m_name] = m_dim_map
        
    logger.info("Dimension prefix map built: %d metrics mapped", len(metric_map))
    _DYNAMIC_DIMENSION_MAP = metric_map
    global _DYNAMIC_METRIC_ENTITIES, _DYNAMIC_OFFSET_GRAINS
    _DYNAMIC_METRIC_ENTITIES = entity_map
    _DYNAMIC_OFFSET_GRAINS = offset_grains
    return metric_map


def _latest_period_with_data(pool, metric: str) -> str | None:
    """
    Newest value of *metric*'s physical time column that actually has rows.

    Deliberately NOT ``MAX(period_month)``: that lands on the phantom trailing
    month described above. The measure expression is applied so an all-inactive
    period cannot win.
    """
    time_col = SQLGenerator._METRIC_TIME_COL.get(metric)
    if not pool or not time_col:
        return None
    try:
        rows = pool.execute(
            f"SELECT MAX({time_col}) AS mx FROM STREAMING_ANALYTICS.marts.fct_mrr_monthly "
            "WHERE is_active = TRUE"
        )
    except Exception as exc:
        logger.warning("Could not resolve latest period for '%s': %s", metric, exc)
        return None
    if not rows:
        return None
    raw = rows[0].get("MX") or rows[0].get("mx")
    return str(raw)[:10] if raw else None


def resolve_mf_order(intent, mapped_dims: list[str]) -> str | None:
    """
    Resolve ``intent.order_by`` / ``intent.order_direction`` into a single
    MetricFlow ``--order`` token, or ``None`` when no valid ordering exists.

    MetricFlow's convention (verified against ``dbt_metricflow/cli/utils.py`` and
    ``metricflow_semantics/query/query_parser.py::_parse_order_by_names``) is a
    bare name for ASC and a ``-`` prefix for DESC.

    Returning ``None`` is what makes ``format_mf_query`` DROP the limit, and that
    is deliberate. A ``LIMIT`` with no ``ORDER BY`` returns an arbitrary row, so
    "which country has the highest MRR" answered with whichever row the engine
    emitted first and the narrative reported it as the maximum. Over-returning
    every row is broad but correct; returning one unordered row is confidently
    wrong.

    The order target MUST already be in the query -- MetricFlow rejects an
    ``--order`` naming something that is neither a selected metric nor a
    group-by -- so it is validated against ``intent.metrics`` and the ALREADY
    entity-mapped ``mapped_dims``, never the raw names off the LLM.

    Args:
        intent:      QueryIntent carrying order_by / order_direction / metrics.
        mapped_dims: Group-by names as they will be sent to MetricFlow, i.e.
                     after entity-prefix mapping.

    Returns:
        e.g. ``"-mrr"`` (descending), ``"subscriber__country"`` (ascending), or
        ``None`` if there is nothing valid to order by.
    """
    raw = (getattr(intent, "order_by", None) or "").strip()
    if not raw:
        return None

    # A model that ignores order_direction sometimes inlines MetricFlow's own
    # "-" convention instead. Accept it rather than reading "-mrr" as a column.
    descending = raw.startswith("-")
    if descending:
        raw = raw[1:].strip()

    direction = (getattr(intent, "order_direction", None) or "").strip().lower()
    if direction in ("desc", "descending"):
        descending = True
    elif direction in ("asc", "ascending"):
        descending = False
    elif not direction and not descending:
        # No direction anywhere. Default DESC: order_by is only ever emitted for
        # ranking questions and "highest/top/most" is overwhelmingly the common
        # one -- "lowest" is the marked case the prompt calls out explicitly.
        # The narrative now states the direction it was given, so a wrong default
        # reads as a visibly odd claim rather than as a silent arbitrary row.
        descending = True

    target: str | None = None
    if raw in (intent.metrics or []):
        target = raw
    else:
        bare_raw = _bare_dimension_name(raw)
        for dim in mapped_dims:
            if dim == raw or _bare_dimension_name(dim) == bare_raw:
                target = dim
                break

    if target is None:
        logger.warning(
            "order_by=%r matches no selected metric %r or group-by %r -- dropping "
            "the ordering (and therefore any limit).",
            raw, intent.metrics, mapped_dims,
        )
        return None

    return "-" + target if descending else target


def default_snapshot_time_range(metric: str, time_range, pool):
    """
    Give a snapshot metric its latest period when the caller supplied no range.

    Returns *time_range* unchanged for every other metric, and whenever the user
    did state a period -- an explicit "in 2025" must always win over the default.
    """
    if time_range is not None or metric not in _SNAPSHOT_METRICS:
        return time_range

    latest = _latest_period_with_data(pool, metric)
    if not latest:
        logger.warning(
            "Snapshot metric '%s' has no time range and the latest period could not "
            "be resolved — the answer will span every period.", metric,
        )
        return None

    from core.intent_extractor import TimeRange

    month_start = latest[:8] + "01"
    logger.info(
        "Snapshot metric '%s' asked without a time range — defaulting to its latest "
        "period (%s). Counting every period would union all months and return "
        "everyone ever active.", metric, month_start,
    )
    return TimeRange(start_date=month_start, end_date=latest, relative=None)


def offset_window_grain(metric: str) -> str | None:
    """Granularity of *metric*'s offset window, or None if it has none."""
    if _DYNAMIC_OFFSET_GRAINS is None:
        build_dimension_prefix_map()
    return (_DYNAMIC_OFFSET_GRAINS or {}).get(metric)


def require_metric_time(metric: str, dimensions: list[str]) -> list[str]:
    """
    Add ``metric_time__<grain>`` when *metric* has an offset window and the
    group-by lacks it.

    Without this the query cannot resolve at all. Observed in production:
    ``net_mrr_growth by subscription__mrr_type`` failed in the warm engine, was
    retried through the CLI subprocess for the identical error, then hit the
    governed builder, which rejects the metric outright because a flat SELECT
    cannot express a month-over-month offset. Net result was a 500 after 33.8s
    for a question the semantic layer can answer in about 1.3s once
    metric_time__month is present.

    The grain comes from the offset window itself, so a metric offset by a week
    would get metric_time__week rather than a hard-coded month.
    """
    grain = offset_window_grain(metric)
    if not grain:
        return dimensions

    dims = list(dimensions or [])
    if any(d.split("__", 1)[0] == "metric_time" for d in dims):
        return dims

    injected = f"metric_time__{grain}"
    logger.info(
        "Metric '%s' has a %s offset window, which cannot resolve without a time "
        "grain — adding '%s' to the group-by.", metric, grain, injected,
    )
    return [injected] + dims


def metric_entities(metric: str) -> set[str]:
    """Entity names reachable from *metric*'s semantic models (primary + foreign)."""
    if _DYNAMIC_METRIC_ENTITIES is None:
        build_dimension_prefix_map()
    return (_DYNAMIC_METRIC_ENTITIES or {}).get(metric, set())


def correct_dimension_entity(dim: str, metric: str) -> str:
    """
    Fix an entity prefix that is not reachable from *metric*.

    The LLM picks the prefix, and it picks a plausible-looking wrong one often
    enough to matter: `total_subscribers by subscription__plan_type`. That metric
    lives on sem_subscribers, whose entity is `subscriber`, so MetricFlow rejects
    it and the query falls through to the governed builder, which answered from
    fct_mrr_monthly and returned **20,086** (a count of subscriptions) where the
    right answer was **16,460** subscribers. Status was `success` and nothing
    logged a problem, which is the worst shape a wrong answer can take.

    A prefix is only rewritten when its entity is genuinely unreachable. Anything
    valid is left exactly as given, so an explicitly configured warmup_matrix
    value or a deliberate choice between two reachable entities still stands.
    """
    if "__" not in dim:
        return dim
    entity = dim.split("__", 1)[0]
    if entity in _RESERVED_DIM_PREFIXES:
        return dim
    reachable = metric_entities(metric)
    if not reachable or entity in reachable:
        return dim

    bare = _bare_dimension_name(dim)
    corrected = (build_dimension_prefix_map().get(metric) or {}).get(bare)
    if not corrected or corrected == dim:
        logger.warning(
            "Dimension '%s' uses entity '%s', which is not reachable from metric "
            "'%s' (reachable: %s). No mapping for bare name '%s' — passing through.",
            dim, entity, metric, sorted(reachable), bare,
        )
        return dim

    logger.warning(
        "Dimension '%s' uses entity '%s', which is not reachable from metric '%s' "
        "— correcting to '%s'.", dim, entity, metric, corrected,
    )
    return corrected



class GeneratedQuery(BaseModel):
    """
    The output of SQLGenerator.generate().

    Contains both the MetricFlow CLI command (for auditability) and the
    compiled SQL returned by the ``--explain`` flag.
    """

    metricflow_query: str
    compiled_sql: str
    metrics: list[str]
    dimensions: list[str]
    time_range: Any | None = None  # TimeRange | None
    grain: str = ""
    estimated_row_count: int | None = None
    sql_review: dict | None = None  # Result from _review_sql()


class SQLGenerator:
    """
    Generates governed SQL via MetricFlow CLI (``mf query --explain``).

    The MetricFlow CLI must be installed in the same virtual environment.
    Results are pure MetricFlow-compiled SQL — no ad-hoc SQL is ever
    constructed by hand.

    Usage::

        generator = SQLGenerator(settings, pool)
        gen_query = generator.generate(intent, validation)
        rows = generator.execute_query(gen_query.compiled_sql)
    """

    # Physical date column per metric — used by the template cache to inject time filters.
    _METRIC_TIME_COL: dict[str, str] = {
        "mrr":                    "period_month",
        "expansion_mrr":          "period_month",
        "total_revenue":          "payment_date",
        "ltv":                    "payment_date",
        "engagement_rate":        "session_start",
        "avg_watch_time":         "session_start",
        "total_watch_time":       "session_start",
        "total_sessions":         "session_start",
        "avg_buffering_events":   "session_start",
        "total_buffering_events": "session_start",
        # churn_rate/retention_rate live on fct_mrr_monthly (monthly event-based
        # definition) — time filters select the month churn HAPPENED, not signup.
        "churn_rate":             "period_month",
        "retention_rate":         "period_month",
        "total_subscribers":      "period_month",
        # churn_date, NOT signup_date — see sem_subscribers.yml's agg_time_dimension.
        # This map is also the single source of truth for _build_fallback_sql's time
        # column; it previously disagreed with an inline if/elif chain there, and both
        # copies were wrong in the same way. test_fallback_time_columns_match_semantic_layer
        # now pins every entry against the semantic YAML.
        "churned_subscribers":    "churn_date",
        "recommendation_ctr":     "event_timestamp",
        "total_recommendations":  "event_timestamp",
        "clicked_recommendations":"event_timestamp",
    }

    def __init__(
        self,
        settings: "Settings",
        pool=None,
        template_cache: SQLTemplateCache | None = None,
        warm_engine=None,
    ) -> None:
        self._settings = settings
        self._pool = pool                        # SnowflakePool — injected at startup; None = legacy mode
        self._template_cache = template_cache    # SQLTemplateCache — injected at startup; None = disabled
        # WarmMetricFlowEngine. Built lazily on the first compile rather than at
        # startup: it costs ~15s and ~100 MB, and a process that never misses the
        # template cache should never pay either. Tests inject a stub directly.
        self._warm_engine = warm_engine
        self._warm_engine_attempted = warm_engine is not None
        self._warm_engine_lock = threading.Lock()

    # ──────────────────────────────────────────────── public

    def generate(
        self,
        intent: "QueryIntent",
        validation: "ValidationResult",
    ) -> GeneratedQuery:
        """
        Build the MetricFlow CLI query string from intent and execute it
        with ``--explain`` to retrieve governed SQL without running it.

        Args:
            intent: Validated query intent.
            validation: Passed ValidationResult (must be safe_to_execute=True).

        Returns:
            :class:`GeneratedQuery` with both the mf command and compiled SQL.

        Raises:
            SQLGenerationError: If MetricFlow CLI fails or returns no SQL.
        """
        # ── Normalise entity prefixes BEFORE anything reads the names ─────────
        # Done here rather than in format_mf_query() because the names are read by
        # the post_filter split, the template-cache key and the outer predicate.
        # Correcting later would leave the outer predicate referencing a column the
        # compiled SQL no longer emits, which is a fresh way to return zero rows.
        _primary = intent.metrics[0] if intent.metrics else ""
        if _primary:
            intent.dimensions = [
                correct_dimension_entity(d, _primary) for d in (intent.dimensions or [])
            ]
            for _f in intent.filters or []:
                _f.column = correct_dimension_entity(_f.column, _primary)
            intent.dimensions = require_metric_time(_primary, intent.dimensions)
            intent.time_range = default_snapshot_time_range(
                _primary, intent.time_range, self._pool
            )

        # A ranked query compiles to SQL carrying ORDER BY / LIMIT, and the L1
        # key is metric x dimensions ONLY (SQLTemplateCache.make_key) -- so
        # caching it would serve a 1-row top-N template to every later
        # "MRR by country". Skipped in BOTH directions: a HIT would also
        # silently discard the limit, because the cached template has none.
        _is_ranked = bool(intent.limit) or bool(getattr(intent, "order_by", None))

        # ── SQL Template Cache check ──────────────────────────────────────────
        # If we have a cached compiled SQL template for this metric+dimension
        # combination, skip the MetricFlow subprocess entirely and inject the
        # time range directly.  This cuts first-query latency from ~45 s to ~1 s
        # for any metric+dim combo seen before (regardless of time range).
        compiled_sql: str | None = None
        used_template_cache = False

        # ── Partition filters: post-aggregation vs. must-reach-MetricFlow ──────
        # A filter on a column that is ALSO a group-by dimension is special: the
        # compiled SQL already SELECTs that column, so the predicate can be applied
        # to the *outer* result instead of being compiled in. That means the query
        # stays eligible for the L1 template cache AND the filter still narrows.
        #
        # This used to unconditionally DROP such filters as "redundant", which was
        # only true for the LLM's habit of enumerating every value of the dimension
        # it groups by ("churn by plan type" → plan_type IN (basic,standard,premium)).
        # For a genuinely narrowing filter it silently widened the answer: asking
        # "how many churned in 2025 for country US" returned all 15 countries.
        post_filters: list = []       # applied as an outer WHERE on the compiled SQL
        effective_filters: list = []  # compiled in by MetricFlow / fallback builder
        if intent.filters:
            global_dim_map = build_dimension_prefix_map()
            primary_metric = intent.metrics[0] if intent.metrics else ""
            dim_map = global_dim_map.get(primary_metric, {})
            for f in intent.filters:
                col = f.column
                if "__" not in col:
                    if col in dim_map:
                        col = dim_map[col]
                    elif _bare_dimension_name(col) in dim_map:
                        col = dim_map[_bare_dimension_name(col)]
                if col in (intent.dimensions or []):
                    post_filters.append((col, f))
                    logger.info(
                        "Filter on '%s' is also a group-by dimension — applying it as an "
                        "outer predicate so the template cache stays usable.", col,
                    )
                    continue

                effective_filters.append(f)

        # Override the intent filters so format_mf_query receives the clean list.
        # post_filters are deliberately excluded here and re-applied uniformly
        # after compilation, so every path (cache hit, MetricFlow, fallback) gets
        # exactly one copy of the predicate.
        intent.filters = effective_filters

        mf_command: list[str] = []
        mf_success = False
        used_fallback_builder = False

        # ── MetricFlow FIRST, via the warm in-process engine ──────────────────────
        # The semantic layer is the source of truth, and compiling from it costs
        # 59-96 ms in production — against a 1.5-2 s Snowflake round trip, that is
        # noise. Serving the cache first bought ~80 ms and cost correctness: a
        # committed template can be stale or (as shipped once) truncated, and only
        # the manual regenerate-and-commit workflow kept it honest. Compiling every
        # query removes that whole class of problem, and a semantic-layer change now
        # takes effect on deploy without an artifact to remember to rebuild.
        #
        # Deliberately the ENGINE ONLY. If it is unavailable we fall through to the
        # cache and then the subprocess — a ~30 s subprocess must never become the
        # primary path just because the engine failed to build.
        if intent.metrics:
            mf_command = self.format_mf_query(intent)
            try:
                compiled_sql = self._compile_with_warm_engine(mf_command)
                mf_success = compiled_sql is not None
            except Exception as exc:
                if is_deterministic_mf_error(exc):
                    logger.warning(
                        "MetricFlow cannot resolve this query (%s) — skipping the "
                        "subprocess and going straight to the template cache.",
                        str(exc).splitlines()[0] if str(exc) else exc,
                    )
                else:
                    logger.warning(
                        "Warm MetricFlow engine failed (%s) — falling back to the "
                        "template cache, then the subprocess.", exc,
                    )
                compiled_sql = None

        # Filtered queries are NOT eligible for the template cache — the compiled SQL
        # contains hard-coded WHERE predicates (e.g., country = 'US') that cannot be
        # reused for a different filter value or an unfiltered version of the same query.
        if (
            compiled_sql is None
            and self._template_cache is not None
            and intent.metrics
            and not effective_filters
            and not _is_ranked
        ):
            cached_tpl = self._template_cache.get(intent.metrics, intent.dimensions)
            if cached_tpl is not None:
                tpl_sql = cached_tpl["sql_template"]

                if cached_tpl.get("has_time_filter", False):
                    # Template requires dates. If the user didn't provide any (all-time),
                    # we inject a massive date range to simulate all-time without breaking the SQL.
                    _req_start = intent.time_range.start_date if intent.time_range else "2000-01-01"
                    _req_end = intent.time_range.end_date if intent.time_range else "2039-12-31"

                    _primary_metric = intent.metrics[0] if intent.metrics else ""
                    _time_col = SQLGenerator._METRIC_TIME_COL.get(_primary_metric, "")
                    
                    _sql_start = apply_grain_rounding(_req_start, _time_col, is_start=True)
                    _sql_end = apply_grain_rounding(_req_end, _time_col, is_start=False)
                    
                    compiled_sql = restore_sql_dates(
                        tpl_sql,
                        _sql_start,
                        _sql_end,
                        style=cached_tpl.get("date_style", "plain"),
                    )
                    logger.info(
                        "SQLTemplateCache HIT (parameterized, style=%s) — skipping MetricFlow. "
                        "User requested %s..%s → grain-adjusted %s..%s.",
                        cached_tpl.get("date_style", "plain"),
                        _req_start, _req_end, _sql_start, _sql_end
                    )
                    used_template_cache = True
                else:
                    if intent.time_range:
                        # User wants dates, but template lacks placeholders.
                        # Force a MetricFlow re-run to get a properly dated query.
                        compiled_sql = None
                        logger.info("SQLTemplateCache MISS — template lacks time filter placeholders.")
                    else:
                        # User wants no dates, and template has no dates. Use as-is.
                        compiled_sql = tpl_sql
                        logger.info("SQLTemplateCache HIT — no time range injection needed.")
                        used_template_cache = True

        # ── Option B: filtered queries skip the ~30 s MetricFlow subprocess ──────
        # Only reachable when the warm engine is unavailable. A filter makes a query
        # ineligible for the L1 template cache, so without an engine it would pay
        # MetricFlow's full ~30 s cold start. The governed fallback builder serves it
        # in sub-millisecond time instead — but it is a hand-written re-implementation
        # of the metric (see the churn_date/signup_date divergence), so it is a last
        # resort, not a design choice. With the engine running, MetricFlow compiles
        # the filter itself via --where and this branch never executes.
        if compiled_sql is None and effective_filters:
            try:
                compiled_sql = self._build_fallback_sql(intent)
                used_fallback_builder = True
                logger.info(
                    "Filtered query — serving from in-process governed fallback builder "
                    "(skipping MetricFlow)."
                )
            except SQLGenerationError as exc:
                logger.info(
                    "Fallback builder cannot serve %s (%s); routing to MetricFlow instead.",
                    intent.metrics, exc.message,
                )

        if compiled_sql is None:
            # Last resort: the `mf` CLI subprocess (~30 s). Reached only when the warm
            # engine is unavailable AND the cache missed AND the fallback builder could
            # not serve this metric.
            if not mf_command:
                mf_command = self.format_mf_query(intent)
            logger.info("Compiling via MetricFlow subprocess: %s", " ".join(mf_command))

            try:
                compiled_sql = self._compile_metricflow(mf_command)
                mf_success = True
            except Exception as exc:
                if isinstance(exc, SQLGenerationError):
                    raise
                logger.warning("MetricFlow execution failed: %s. Falling back to governed SQL template.", exc)
                # May raise SQLGenerationError for an unmapped metric (e.g. net_mrr_growth);
                # that surfaces a clear error rather than a cryptic Snowflake crash.
                compiled_sql = self._build_fallback_sql(intent)
                mf_success = False

        grain = ""
        if intent.metrics:
            # Grain is resolved upstream by the registry; we embed it as a comment
            grain = "subscription+month" if "mrr" in intent.metrics[0].lower() else "record"

        time_range = intent.time_range

        # ── Adversarial SQL review ─────────────────────────────────────────────
        # Skip the reviewer on template cache hits or when MetricFlow succeeds natively
        review_result: dict
        if used_template_cache:
            review_result = {"approved": True, "sql": compiled_sql, "source": "template_cache"}
        elif used_fallback_builder:
            # Option B path: deterministic, column-validated, value-escaped SQL from the
            # governed builder — the adversarial LLM reviewer (≈1-2 s + rewrite risk) is
            # unnecessary here. It still runs on the MetricFlow-failure path below, where
            # SQL provenance is less certain.
            review_result = {"approved": True, "sql": compiled_sql, "source": "fallback_builder"}
        else:
            if mf_success:
                logger.info("MetricFlow generated valid SQL. Bypassing LLM review.")
                review_result = {"approved": True, "sql": compiled_sql, "source": "metricflow_native"}
            else:
                # mf failed, use fallback sql with LLM review applied
                review_result = self._review_sql(compiled_sql)
                if not review_result.get("approved", True):
                    revised = review_result.get("revised_sql")
                    if revised and self._validate_revised_sql(revised):
                        logger.warning("SQL reviewer found issues on fallback_sql; using revised SQL. Issues: %s", review_result.get("issues"))
                        compiled_sql = revised
                    elif revised:
                        logger.warning(
                            "SQL reviewer revised SQL REJECTED by safety check — "
                            "using original fallback SQL. Issues: %s",
                            review_result.get("issues"),
                        )
                    else:
                        logger.warning("SQL reviewer found issues on fallback_sql but could not auto-revise. Issues: %s", review_result.get("issues"))

            # ── Store the REVIEWED SQL in the template cache ──────────────────────
            # We store AFTER the reviewer so the cached template already contains
            # the hygiene WHERE clause (is_active, plan_type IN …) that the
            # reviewer adds.  This guarantees that inject_time_filter can find a
            # WHERE to anchor to instead of appending after GROUP BY.
            # CRITICAL: We ONLY cache if mf_success is True. We never cache
            # fallback SQL to avoid poisoning the cache with LLM hallucinations.
            # CRITICAL: We ONLY cache if mf_success is True AND the query has no
            # filters. Filtered SQL is query-specific and must not be reused.
            if (
                self._template_cache is not None
                and intent.metrics
                and mf_success
                and not intent.filters
                and not _is_ranked
            ):
                try:
                    sql_template = compiled_sql
                    has_placeholder = False
                    date_style = "plain"

                    if intent.time_range:
                        # Strategy 1: search for user-provided dates directly.
                        # Works for day-grain metrics where MetricFlow embeds them as-is.
                        sql_template, has_placeholder, date_style = parameterize_sql_dates(
                            compiled_sql,
                            intent.time_range.start_date,
                            intent.time_range.end_date,
                        )

                        if not has_placeholder:
                            # Strategy 2: auto-extraction.
                            # MetricFlow grain-adjusted the dates before embedding them
                            # (e.g. monthly-grain mrr: 2026-03-19 → 2026-03-01).
                            # Scan the SQL for whatever date literals MetricFlow used.
                            _primary = intent.metrics[0] if intent.metrics else ""
                            _tcol = SQLGenerator._METRIC_TIME_COL.get(_primary, "")
                            sql_template, has_placeholder, date_style = (
                                parameterize_by_auto_extraction(compiled_sql, _tcol)
                            )

                    self._template_cache.set(
                        intent.metrics,
                        intent.dimensions,
                        sql_template,
                        has_placeholder,
                        date_style=date_style,
                    )
                except Exception as tpl_exc:
                    logger.warning(
                        "Failed to store SQL template (non-fatal): %s", tpl_exc
                    )

        # ── Re-apply group-by-dimension filters as an outer predicate ─────────────
        # Deliberately AFTER the template-cache write above: the cache must keep the
        # reusable *unfiltered* template, or a "country = US" request would poison
        # the shared `metric × country` key and every later breakdown would return
        # only the US row. Applied once here, so it lands regardless of which path
        # (cache hit / MetricFlow / fallback builder) produced the SQL.
        if post_filters and compiled_sql:
            compiled_sql = wrap_with_outer_predicates(compiled_sql, post_filters)
            logger.info(
                "Applied %d outer predicate(s) for group-by-dimension filter(s): %s",
                len(post_filters), [c for c, _ in post_filters],
            )

        # Audit label — reflect which path actually produced the SQL. Order matters:
        # mf_command is now built for EVERY query (MetricFlow runs first), so it can
        # no longer be used to infer which path won. Check the specific flags first
        # or a fallback-builder result gets labelled as native MetricFlow in the
        # provenance we show the user.
        if used_fallback_builder:
            _filter_cols = ",".join(f.column for f in (intent.filters or []))
            _mf_cmd = (
                f"[fallback_builder] metrics={','.join(intent.metrics)} "
                f"dims={','.join(intent.dimensions)} filters={_filter_cols}"
            )
        elif used_template_cache:
            _mf_cmd = (
                f"[template_cache] mf query --metrics {','.join(intent.metrics)} "
                f"--group-by {','.join(intent.dimensions)} --explain"
            )
        elif mf_command:
            _mf_cmd = " ".join(mf_command)
        else:
            _mf_cmd = (
                f"mf query --metrics {','.join(intent.metrics)} "
                f"--group-by {','.join(intent.dimensions)} --explain"
            )

        return GeneratedQuery(
            metricflow_query=_mf_cmd,
            compiled_sql=compiled_sql,
            metrics=intent.metrics,
            dimensions=intent.dimensions,
            time_range=time_range,
            grain=grain,
            estimated_row_count=None,
            sql_review=review_result,
        )

    def _get_warm_engine(self):
        """
        Return the warm MetricFlow engine, building it on first need.

        Both success and failure are memoised: a deployment where the engine
        cannot build must not re-pay the ~15s attempt on every cache miss. The
        lock makes the build happen once even if several ``anyio`` worker threads
        miss the template cache simultaneously.

        Returns:
            A ``WarmMetricFlowEngine``, or ``None`` if disabled or unavailable.
        """
        if self._warm_engine_attempted:
            return self._warm_engine

        with self._warm_engine_lock:
            if self._warm_engine_attempted:  # another thread built it while we waited
                return self._warm_engine
            self._warm_engine_attempted = True

            if not getattr(self._settings, "metricflow_in_process", False):
                logger.info(
                    "In-process MetricFlow disabled by config — using the `mf` subprocess."
                )
                return None

            from core.metricflow_engine import WarmMetricFlowEngine

            project_dir = getattr(self._settings, "dbt_project_dir", "")
            if not project_dir:
                logger.warning("dbt_project_dir is unset — cannot build the warm engine.")
                return None

            # RAM before/after the build. This is the one measurement worth having
            # on a 512 MB instance: config.py documents ~100 MB resident for the
            # engine, and this is where that claim becomes observable. Logging
            # here rather than in main.py's pre-warm thread covers BOTH entry
            # paths — startup pre-warm and the lazy first-miss build.
            from core import memory

            rss_before = memory.rss_mb()
            build_started = time.perf_counter()

            self._warm_engine = WarmMetricFlowEngine.try_build(
                dbt_project_dir=project_dir,
                dbt_profiles_dir=project_dir,
            )

            memory.log_status(
                "MetricFlow engine built in %.1fs (%s)"
                % (
                    time.perf_counter() - build_started,
                    "ok" if self._warm_engine is not None else "FAILED",
                ),
                target_logger=logger,
                baseline_mb=rss_before,
            )
            return self._warm_engine

    def _compile_with_warm_engine(self, mf_command: list[str]) -> str | None:
        """
        Compile via the warm in-process engine ONLY — never the subprocess.

        This backs the primary path, where falling through to a ~30 s subprocess
        would be worse than using a possibly-stale cached template. Returns
        ``None`` when no engine is available so the caller can try the cache;
        propagates engine errors so the caller can decide.

        Args:
            mf_command: argv list from :meth:`format_mf_query`.

        Returns:
            Compiled SQL, or ``None`` if there is no warm engine.
        """
        engine = self._get_warm_engine()
        if engine is None:
            return None

        started = time.perf_counter()
        sql = engine.explain_argv(mf_command)
        logger.info(
            "MetricFlow compiled in-process in %.0f ms (no subprocess).",
            (time.perf_counter() - started) * 1000,
        )
        return sql

    def _compile_metricflow(self, mf_command: list[str]) -> str:
        """
        Compile *mf_command* to SQL, preferring the warm in-process engine.

        The engine and the subprocess are given the identical argv list, so the
        two paths cannot produce different SQL. If the engine is absent or
        raises, we fall through to the subprocess: slower, but never wrong.

        Args:
            mf_command: argv list from :meth:`format_mf_query`.

        Returns:
            Compiled SQL.
        """
        engine = self._get_warm_engine()
        if engine is not None:
            started = time.perf_counter()
            try:
                sql = engine.explain_argv(mf_command)
                logger.info(
                    "MetricFlow compiled in-process in %.0f ms (no subprocess).",
                    (time.perf_counter() - started) * 1000,
                )
                return sql
            except Exception as exc:
                if is_deterministic_mf_error(exc):
                    # The CLI would reach the identical verdict. Raise now and let
                    # the caller fall through to the template cache / builder.
                    logger.warning(
                        "MetricFlow rejected the query itself (%s) — not retrying "
                        "via subprocess, the result would be identical.",
                        str(exc).splitlines()[0] if str(exc) else exc,
                    )
                    raise
                logger.warning(
                    "Warm MetricFlow engine failed (%s) — retrying via subprocess.", exc,
                )

        return self._run_mf_subprocess(mf_command)

    def _run_mf_subprocess(self, mf_command: list[str]) -> str:
        """Run MetricFlow subprocess and return the extracted SQL string.

        Uses ``shell=False`` (argv list) to prevent shell injection via
        LLM-derived dates and filter values.
        """
        env = os.environ.copy()
        env["DBT_PROJECT_DIR"] = "../dbt_streaming_analytics/streaming_analytics"
        env["DBT_PROFILES_DIR"] = "../dbt_streaming_analytics/streaming_analytics"

        s = self._settings
        env["SNOWFLAKE_ACCOUNT"] = s.snowflake_account
        env["SNOWFLAKE_USER"] = s.snowflake_user
        env["SNOWFLAKE_PASSWORD"] = s.snowflake_password
        env["SNOWFLAKE_DATABASE"] = s.snowflake_database
        env["SNOWFLAKE_WAREHOUSE"] = s.snowflake_warehouse
        env["SNOWFLAKE_ROLE"] = s.snowflake_role
        env["SNOWFLAKE_SCHEMA"] = s.snowflake_schema
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"

        # Human-readable command string for logging / error messages only.
        mf_command_str = " ".join(mf_command)

        try:
            result = subprocess.run(
                mf_command,
                shell=False,
                capture_output=True,
                text=True,
                # The env already sets PYTHONUTF8=1 so the CHILD writes UTF-8, but
                # text=True decodes with the PARENT's locale encoding. On Windows
                # that is cp1252, and the mf CLI's spinner glyphs (U+2807 and
                # friends) raise UnicodeDecodeError inside subprocess's reader
                # thread. stdout then arrives as None and the caller dies with
                # "'NoneType' object has no attribute 'splitlines'", which reads
                # like a MetricFlow fault and is really a decoding one. errors=
                # "replace" keeps a mangled spinner from destroying valid SQL.
                encoding="utf-8",
                errors="replace",
                timeout=120,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise SQLGenerationError(
                "MetricFlow CLI timed out after 120 seconds.",
                mf_command=mf_command_str,
                stderr="timeout",
            ) from exc

        if result.returncode != 0:
            raise Exception(f"MetricFlow CLI returned non-zero exit code {result.returncode}.\nSTDERR: {result.stderr}\nSTDOUT: {result.stdout}")

        return self._extract_sql_from_mf_output(result.stdout, mf_command_str)

    def format_mf_query(self, intent: "QueryIntent") -> list[str]:
        """
        Build the ``mf query`` CLI argv list from a QueryIntent.

        Returns a list of strings suitable for ``subprocess.run(..., shell=False)``.
        All LLM-derived values (dates, filter values) are validated before
        inclusion to prevent shell/command injection.

        Handles:
        - Multiple metrics (comma-separated)
        - Multiple group-by dimensions (comma-separated)
        - Time range as ``--start-time`` / ``--end-time`` flags
        - Limit via ``--limit``
        - ``--explain`` flag to return SQL without executing

        Args:
            intent: Query intent with metrics, dimensions, time_range.

        Returns:
            Argv list — e.g. ``["mf", "query", "--metrics", "mrr", "--explain"]``.
        """
        parts: list[str] = ["mf", "query"]

        # Hoisted out of the `if intent.dimensions:` block below: the ranking
        # resolution has to validate order_by against the SAME entity-mapped
        # names MetricFlow will group by, not the raw ones off the LLM.
        mapped_dims: list[str] = []

        if intent.metrics:
            parts.extend(["--metrics", ",".join(intent.metrics)])

        if intent.dimensions:
            global_dim_map = build_dimension_prefix_map()
            
            # Use the first metric to resolve prefixes (MetricFlow relies on primary metric's model)
            primary_metric = intent.metrics[0] if intent.metrics else ""
            dim_map = global_dim_map.get(primary_metric, {})
            
            for dim in intent.dimensions:
                # If the dimension is already entity-prefixed (e.g. "payment__payment_method",
                # "subscriber__plan_type") trust it as-is and skip the mapper.
                # This respects explicitly configured warmup_matrix values and avoids
                # the mapper silently switching prefixes on already-correct inputs.
                if "__" in dim:
                    mapped_dims.append(dim)
                elif dim in dim_map:
                    mapped_dims.append(dim_map[dim])
                elif _bare_dimension_name(dim) in dim_map:
                    mapped_dims.append(dim_map[_bare_dimension_name(dim)])
                else:
                    logger.warning("Dimension '%s' not found for metric '%s', passing raw to MetricFlow.", dim, primary_metric)
                    mapped_dims.append(dim)
            parts.extend(["--group-by", ",".join(mapped_dims)])

        if intent.time_range:
            _validate_date(intent.time_range.start_date, "start_date")
            _validate_date(intent.time_range.end_date, "end_date")
            parts.extend(["--start-time", intent.time_range.start_date])
            parts.extend(["--end-time", intent.time_range.end_date])

        if intent.filters:
            import json as _json
            global_dim_map = build_dimension_prefix_map()
            primary_metric = intent.metrics[0] if intent.metrics else ""
            dim_map = global_dim_map.get(primary_metric, {})
            _OP_MAP = {"eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
            where_parts: list[str] = []
            for f in intent.filters:
                col = f.column
                # Resolve to fully-prefixed MetricFlow dimension name
                if "__" not in col:
                    if col in dim_map:
                        col = dim_map[col]
                    elif _bare_dimension_name(col) in dim_map:
                        col = dim_map[_bare_dimension_name(col)]
                    else:
                        logger.warning(
                            "Filter column '%s' not found in dim_map for metric '%s' — passing raw.",
                            col, primary_metric,
                        )

                # ── Guard: skip if this dimension is already in intent.dimensions ──
                # e.g. "show churn by plan type" → plan_type in dims AND in filters
                # (LLM sometimes adds an IN(all_plans) filter redundantly).
                if col in (intent.dimensions or []):
                    logger.info(
                        "Skipping filter on '%s' — already used as a group-by dimension.", col
                    )
                    continue
                
                # ── Guard: skip time-based filters if time_range handles it ──
                if intent.time_range and any(t in col.lower() for t in ("time", "date", "month", "year", "day", "quarter", "week")):
                    logger.info("Skipping time-based filter on '%s' because time_range is set.", col)
                    continue

                if f.operator == "in":
                    # Robustly parse the value — LLM may return a real list OR a
                    # string that looks like a Python/JSON list: "['a','b','c']"
                    raw_list = f.value
                    if isinstance(raw_list, str):
                        # Try JSON first, then ast.literal_eval as fallback
                        import ast as _ast
                        try:
                            parsed = _json.loads(raw_list)
                            raw_list = parsed if isinstance(parsed, list) else [parsed]
                        except (_json.JSONDecodeError, ValueError):
                            try:
                                parsed = _ast.literal_eval(raw_list)
                                raw_list = parsed if isinstance(parsed, list) else [parsed]
                            except Exception:
                                raw_list = [raw_list]  # treat whole string as one value
                    vals = raw_list if isinstance(raw_list, list) else [raw_list]
                    # Sanitize each filter value (defence-in-depth)
                    vals = [_sanitize_filter_value(str(v)) for v in vals]
                    val_str = ", ".join(f"'{v}'" for v in vals)
                    # MetricFlow --where requires Jinja templating: without {{ }} the
                    # expression is passed verbatim into the compiled SQL, and Snowflake
                    # fails with "Unknown function DIMENSION".
                    where_parts.append(f"{{{{ Dimension('{col}') }}}} IN ({val_str})")
                else:
                    op = _OP_MAP.get(f.operator, "=")
                    raw_val = str(f.value)
                    _sanitize_filter_value(raw_val)  # defence-in-depth
                    try:
                        float(raw_val)
                        val_str = raw_val
                    except ValueError:
                        val_str = f"'{raw_val}'"
                    where_parts.append(f"{{{{ Dimension('{col}') }}}} {op} {val_str}")
            if where_parts:
                where_clause = " AND ".join(where_parts)
                parts.extend(["--where", where_clause])
                logger.info("MetricFlow --where clause: %s", where_clause)

        # -- Ranking: --limit is meaningless without --order -----------------
        # intent.order_by was extracted by the LLM and read by NOTHING for the
        # life of this file, while intent.limit WAS applied -- so every
        # superlative question compiled to a LIMIT with no ORDER BY and
        # returned an arbitrary row. See resolve_mf_order() for why dropping
        # the limit is the safe direction to fail.
        order_name = resolve_mf_order(intent, mapped_dims)
        if order_name:
            parts.extend(["--order", order_name])

        if intent.limit:
            if order_name:
                parts.extend(["--limit", str(intent.limit)])
            else:
                logger.warning(
                    "Dropping limit=%s: no usable ORDER BY (order_by=%r). "
                    "Returning every row is broad but correct; one unordered "
                    "row would be reported to the user as the ranked answer.",
                    intent.limit, getattr(intent, "order_by", None),
                )

        # Always use --explain so we get SQL without running it in the warehouse
        parts.append("--explain")

        return parts

    def execute_query(self, compiled_sql: str) -> list[dict[str, Any]]:
        """
        Execute the compiled SQL against Snowflake.

        Uses a pre-opened connection from the shared pool when available
        (eliminates ~2 s per-call connection overhead).  Falls back to
        opening a new connection when the pool is not configured.

        Args:
            compiled_sql: Governed SQL from MetricFlow --explain.

        Returns:
            List of row dicts with column names as keys.

        Raises:
            SnowflakeConnectionError: On connection failure or query timeout.
        """
        # DuckDBPool exposes its own execute(): it enforces the read-only guard and
        # uppercases column names to match Snowflake's DictCursor, so downstream
        # response shaping is unchanged by the engine swap.
        if self._pool is not None and hasattr(self._pool, "execute"):
            return self._pool.execute(compiled_sql)

        # NEVER fall through to the Snowflake direct path when DuckDB is the
        # configured warehouse. _execute_direct() is Snowflake-specific, so when
        # DuckDBPool.initialise() failed at startup (pool=None) the gateway used to
        # silently connect to Snowflake instead — which on Render 2026-08-04 turned
        # a missing warehouse FILE into a stream of "Your free trial has ended"
        # errors, hiding the real cause behind a dead dependency. Fail loudly with
        # the actionable message instead.
        engine = str(getattr(self._settings, "warehouse_engine", "snowflake")).lower()
        if self._pool is None and engine == "duckdb":
            raise SnowflakeConnectionError(
                "DuckDB is the configured warehouse (warehouse_engine=duckdb) but no "
                "connection is available — DuckDBPool.initialise() failed at startup. "
                "Look for 'DuckDB init failed' in the startup log. Usual causes: the "
                f"warehouse file is missing at '{getattr(self._settings, 'duckdb_path', '?')}' "
                "(on Render, the Build Command must be ./build.sh so the release asset "
                "is downloaded), DUCKDB_ASSET_URL is wrong, or `dbt run` was never run "
                "so the file has no 'marts' schema. Refusing to fall back to Snowflake."
            )

        if self._pool is not None:
            return self._execute_with_pool(compiled_sql)
        return self._execute_direct(compiled_sql)

    def _execute_with_pool(self, compiled_sql: str) -> list[dict[str, Any]]:
        """Acquire a pooled connection and run the query."""
        try:
            with self._pool.acquire() as conn:
                cursor = conn.cursor(snowflake.connector.DictCursor)
                cursor.execute(
                    f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {_QUERY_TIMEOUT_SECONDS}"
                )
                cursor.execute(compiled_sql)
                rows: list[dict[str, Any]] = cursor.fetchall()
                logger.info("Query returned %d rows (pooled connection).", len(rows))
                return rows
        except SnowflakeConnectionError:
            raise
        except Exception as exc:
            raise SnowflakeConnectionError(
                f"Snowflake query execution failed: {exc}"
            ) from exc

    def _execute_direct(self, compiled_sql: str) -> list[dict[str, Any]]:
        """Fallback: open a fresh connection (legacy / pool-unavailable path)."""
        s = self._settings
        logger.info("Connecting to Snowflake account='%s' (no pool).", s.snowflake_account)
        try:
            conn = snowflake.connector.connect(
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
        except Exception as exc:
            raise SnowflakeConnectionError(
                f"Failed to connect to Snowflake: {exc}"
            ) from exc
        try:
            cursor = conn.cursor(snowflake.connector.DictCursor)
            cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {_QUERY_TIMEOUT_SECONDS}")
            cursor.execute(compiled_sql)
            rows = cursor.fetchall()
            logger.info("Query returned %d rows.", len(rows))
            return rows
        except Exception as exc:
            raise SnowflakeConnectionError(
                f"Snowflake query execution failed: {exc}"
            ) from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ────────────────────────────────────────────────────── private

    # Allowed tables that the fallback SQL reviewer may reference.
    _ALLOWED_TABLES = re.compile(
        r"\b(STREAMING_ANALYTICS\.(marts|staging|intermediate)\.\w+|fct_\w+|dim_\w+|int_\w+|stg_\w+)\b",
        re.IGNORECASE,
    )

    # DDL/DML keywords that must never appear in revised SQL.
    _FORBIDDEN_SQL = re.compile(
        r"\b(DROP|DELETE|INSERT|UPDATE|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|MERGE|EXEC|EXECUTE|CALL)\b",
        re.IGNORECASE,
    )

    def _validate_revised_sql(self, sql: str) -> bool:
        """Reject LLM-revised SQL that is not a single read-only SELECT.

        This is a defence-in-depth check on the fallback path (Audit issue #5).
        If the LLM reviewer's revised SQL looks unsafe we fall back to the
        original (governed) fallback SQL rather than executing the revision.

        Checks:
          1. Single statement (no ``;`` separating multiple commands).
          2. Starts with ``SELECT`` (after stripping comments).
          3. No DDL/DML keywords (DROP, DELETE, INSERT, etc.).

        Returns:
            ``True`` if the SQL passes all safety checks.
        """
        # Strip SQL comments
        stripped = re.sub(r'--[^\n]*', '', sql).strip()
        stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.DOTALL).strip()

        # 1. Single statement
        if ';' in stripped.rstrip(';'):
            logger.warning("Revised SQL rejected: contains multiple statements.")
            return False

        # 2. Must start with SELECT
        if not stripped.upper().startswith('SELECT'):
            logger.warning("Revised SQL rejected: does not start with SELECT.")
            return False

        # 3. No forbidden DDL/DML keywords
        if self._FORBIDDEN_SQL.search(stripped):
            logger.warning("Revised SQL rejected: contains forbidden DDL/DML keyword.")
            return False

        return True

    def _review_sql(self, sql: str) -> dict:
        """
        Run the SQL through the adversarial sql_reviewer skill before execution.

        Loads ``gateway/skills/sql_reviewer.md`` and calls the LLM with the SQL
        as the user message.  Parses the response for PASS or ISSUES FOUND.

        Args:
            sql: The compiled SQL string to review.

        Returns:
            A dict with one of these shapes:

            Approved::

                {"approved": True, "sql": <original sql>}

            Issues found::

                {
                    "approved": False,
                    "issues": [<list of issue strings>],
                    "revised_sql": <corrected SQL string or None>,
                }

            Reviewer unavailable (fail-open)::

                {"approved": True, "sql": <original sql>, "warning": "reviewer unavailable"}
        """
        if not _SKILL_LOADER_AVAILABLE:
            return {"approved": True, "sql": sql, "warning": "reviewer unavailable (skill_loader not installed)"}

        try:
            reviewer_md = load_skill("sql_reviewer")
        except Exception as exc:
            logger.warning("Could not load sql_reviewer skill: %s", exc)
            return {"approved": True, "sql": sql, "warning": "reviewer unavailable"}

        # Build a lightweight LLM client using the settings already on the instance.
        # We reuse the same OpenAI-compatible pattern used by IntentExtractor.
        try:
            s = self._settings
            # Match the IntentExtractor fallback order: Gemini -> Groq -> OpenRouter.
            # Fail-fast: the reviewer fails open, so a degraded provider must not
            # stall the request on the SDK's default 600 s timeout.
            if getattr(s, "google_api_key", ""):
                review_client = OpenAI(
                    api_key=s.google_api_key,
                    base_url=s.google_base_url,
                    timeout=15.0,
                    max_retries=0,
                )
                review_model = s.google_model
            elif getattr(s, "openai_api_key", ""):
                review_client = OpenAI(
                    api_key=s.openai_api_key,
                    base_url=s.llm_base_url,
                    timeout=15.0,
                    max_retries=0,
                )
                review_model = s.openai_model
            elif getattr(s, "openrouter_api_key", ""):
                review_client = OpenAI(
                    api_key=s.openrouter_api_key,
                    base_url=s.openrouter_base_url,
                    timeout=15.0,
                    max_retries=0,
                )
                review_model = s.openrouter_model
            else:
                raise ValueError("No LLM API keys configured")

            response = review_client.chat.completions.create(
                model=review_model,
                messages=[
                    {"role": "system", "content": reviewer_md},
                    {"role": "user", "content": f"Review this SQL query:\n\n{sql}"},
                ],
                temperature=0.0,
                max_tokens=1024,
            )
            raw = (response.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("SQL reviewer LLM call failed: %s. Failing open.", exc)
            return {"approved": True, "sql": sql, "warning": "reviewer unavailable"}

        if raw.startswith("PASS"):
            return {"approved": True, "sql": sql}

        if raw.startswith("ISSUES FOUND"):
            # Extract numbered issue list (lines starting with a digit + dot/paren)
            import re
            issue_lines = re.findall(r"^\d+[.)].+", raw, re.MULTILINE)

            # Extract REVISED SQL block (everything after "REVISED SQL:" label)
            revised_sql: str | None = None
            revised_marker = "REVISED SQL:"
            marker_pos = raw.find(revised_marker)
            if marker_pos != -1:
                candidate = raw[marker_pos + len(revised_marker):].strip()
                # Strip any markdown code fences
                candidate = re.sub(r"^```[a-z]*\n?", "", candidate, flags=re.IGNORECASE).strip()
                candidate = re.sub(r"\n?```$", "", candidate).strip()
                if candidate and candidate.upper() != "CANNOT AUTO-REVISE — REQUIRES HUMAN REVIEW":
                    revised_sql = candidate

            return {
                "approved": False,
                "issues": issue_lines,
                "revised_sql": revised_sql,
            }

        # Unexpected format — fail open
        logger.warning(
            "SQL reviewer returned unexpected format (first 200 chars): %s",
            raw[:200],
        )
        return {"approved": True, "sql": sql, "warning": "reviewer returned unexpected format"}

    def _extract_sql_from_mf_output(self, stdout: str, mf_command: str) -> str:
        """
        Parse the SQL block from MetricFlow's --explain output.

        MetricFlow outputs text with the SQL after a 'Generated SQL:' or
        'SQL:' header.  We extract everything from the first SELECT onwards.

        Args:
            stdout: The full stdout from MetricFlow CLI.
            mf_command: The CLI command (for error context).

        Returns:
            The extracted SQL string.

        Raises:
            SQLGenerationError: If no SQL could be found in the output.
        """
        # MetricFlow renders complex queries (multi-model ratios like ltv, joined
        # group-bys) as a top-level CTE — `WITH cte AS ( SELECT … )`. Capturing from
        # the first SELECT would drop the `WITH cte AS (` prefix and leave the CTE's
        # closing `)` dangling → Snowflake "syntax error … unexpected ')'". So begin
        # at the first line starting with WITH *or* SELECT, whichever comes first.
        _sql_start = re.compile(r"^(?:WITH|SELECT)\b", re.IGNORECASE)

        lines = stdout.splitlines()
        sql_lines: list[str] = []
        capturing = False

        for line in lines:
            if not capturing and _sql_start.match(line.strip()):
                capturing = True

            if capturing:
                # A blank line (or `mf`'s own emoji-decorated chrome, e.g.
                # "Success 🦄 — query completed…") ends the SQL block ONLY if what
                # we have so far is already a complete statement.
                #
                # The previous version broke at the first blank line unconditionally.
                # MetricFlow puts one between the CTE list and the outer SELECT for
                # multi-CTE queries — ratio metrics grouped by a joined dimension —
                # so `ltv × subscriber__country` was truncated to its CTE: 155 of
                # 1882 chars, invalid SQL, no exception raised, and therefore cached
                # and shipped inside .sql_template_cache.json.
                stripped = line.strip()
                if (not stripped or not stripped.isascii()) and sql_lines:
                    partial = "\n".join(sql_lines)
                    if _has_top_level_select(partial) and partial.count("(") == partial.count(")"):
                        break
                    if not stripped:
                        # Blank line *inside* the statement — keep it so the text
                        # matches what the in-process engine returns.
                        sql_lines.append(line)
                    continue
                sql_lines.append(line)

        sql = "\n".join(sql_lines).strip()

        if not sql:
            # Fallback: slice from the first WITH/SELECT keyword, whichever is earlier.
            m = re.search(r"(?im)^\s*(?:WITH|SELECT)\b", stdout)
            if m:
                sql = stdout[m.start():].strip()

        if not sql:
            raise SQLGenerationError(
                "MetricFlow --explain returned no SQL output.",
                mf_command=mf_command,
                stderr=stdout[:300],
            )

        # Structural guard: a `WITH …` query must have a SELECT outside its CTE
        # parentheses. Failing loudly here is what stops a truncated compile from
        # being cached — the previous silent truncation shipped two invalid
        # templates to production.
        if not _has_top_level_select(sql):
            raise SQLGenerationError(
                "MetricFlow --explain produced SQL with no top-level SELECT — the "
                "output was probably truncated. Refusing to use or cache it.",
                mf_command=mf_command,
                stderr=sql[:300],
            )

        return sql

    def _build_fallback_sql(self, intent: "QueryIntent") -> str:
        """
        Build a representative governed SQL statement for demonstration when
        the MetricFlow CLI is not available.

        This mirrors what MetricFlow would generate for the given intent,
        using the certified mart tables directly.  It is clearly marked
        as a gateway-generated fallback.

        Args:
            intent: The validated query intent.

        Returns:
            A Snowflake-compatible SELECT statement.
        """
        # Map metrics to their source mart tables
        _METRIC_TABLE: dict[str, str] = {
            "mrr": "STREAMING_ANALYTICS.marts.fct_mrr_monthly",
            "expansion_mrr": "STREAMING_ANALYTICS.marts.fct_mrr_monthly",
            "total_revenue": "STREAMING_ANALYTICS.marts.fct_payments",
            "ltv": "STREAMING_ANALYTICS.marts.fct_payments",
            "engagement_rate": "STREAMING_ANALYTICS.marts.fct_stream_sessions",
            "avg_watch_time": "STREAMING_ANALYTICS.marts.fct_stream_sessions",
            "total_watch_time": "STREAMING_ANALYTICS.marts.fct_stream_sessions",
            "total_sessions": "STREAMING_ANALYTICS.marts.fct_stream_sessions",
            "avg_buffering_events": "STREAMING_ANALYTICS.marts.fct_stream_sessions",
            "total_buffering_events": "STREAMING_ANALYTICS.marts.fct_stream_sessions",
            "churn_rate": "STREAMING_ANALYTICS.marts.fct_mrr_monthly",
            "retention_rate": "STREAMING_ANALYTICS.marts.fct_mrr_monthly",
            "total_subscribers": "STREAMING_ANALYTICS.marts.fct_mrr_monthly",
            "churned_subscribers": "STREAMING_ANALYTICS.marts.dim_subscribers",
            "recommendation_ctr": "STREAMING_ANALYTICS.staging.stg_recommendation_events",
            "total_recommendations": "STREAMING_ANALYTICS.staging.stg_recommendation_events",
            "clicked_recommendations": "STREAMING_ANALYTICS.staging.stg_recommendation_events",
        }

        _METRIC_EXPR: dict[str, str] = {
            "mrr": "SUM(mrr_usd) AS mrr",
            "expansion_mrr": "SUM(CASE WHEN mrr_type = 'expansion' THEN mrr_change_usd ELSE 0 END) AS expansion_mrr",
            "total_revenue": "SUM(CASE WHEN status = 'succeeded' THEN amount_usd ELSE 0 END) AS total_revenue",
            "ltv": "SUM(CASE WHEN status = 'succeeded' THEN amount_usd ELSE 0 END) AS ltv",
            "engagement_rate": "AVG(completion_pct) AS engagement_rate",
            # Streaming-session metrics on fct_stream_sessions — mirror the
            # sem_stream_sessions measures (duration_minutes, session_id, buffering_events).
            "avg_watch_time": "AVG(duration_minutes) AS avg_watch_time",
            "total_watch_time": "SUM(duration_minutes) AS total_watch_time",
            "total_sessions": "COUNT(session_id) AS total_sessions",
            "avg_buffering_events": "AVG(buffering_events) AS avg_buffering_events",
            "total_buffering_events": "SUM(buffering_events) AS total_buffering_events",
            # Monthly event-based churn on fct_mrr_monthly — mirrors the governed
            # MetricFlow definition and the dashboard churn_rate_kpi formula.
            "churn_rate": (
                "COUNT(DISTINCT CASE WHEN mrr_type = 'churned' THEN subscriber_id END)::FLOAT / "
                "NULLIF(COUNT(DISTINCT CASE WHEN mrr_type != 'inactive' THEN subscriber_id END), 0) AS churn_rate"
            ),
            "retention_rate": (
                "1 - COUNT(DISTINCT CASE WHEN mrr_type = 'churned' THEN subscriber_id END)::FLOAT / "
                "NULLIF(COUNT(DISTINCT CASE WHEN mrr_type != 'inactive' THEN subscriber_id END), 0) AS retention_rate"
            ),
            "total_subscribers": "COUNT(DISTINCT CASE WHEN is_active = TRUE THEN subscriber_id END) AS total_subscribers",
            "churned_subscribers": "COUNT(DISTINCT CASE WHEN is_churned = TRUE THEN subscriber_id END) AS churned_subscribers",
            "recommendation_ctr": (
                "COUNT(CASE WHEN was_clicked = TRUE THEN event_id END)::FLOAT / "
                "NULLIF(COUNT(event_id), 0) AS recommendation_ctr"
            ),
            "total_recommendations": "COUNT(event_id) AS total_recommendations",
            "clicked_recommendations": "COUNT(CASE WHEN was_clicked = TRUE THEN event_id END) AS clicked_recommendations",
        }

        # Semantic layer dimension name → physical Snowflake column name.
        # The intent extractor returns semantic names (e.g. 'event_timestamp');
        # the fallback SQL must use the real column names from the source table.
        _DIM_COLUMN_MAP: dict[str, str] = {
            "event_timestamp": "event_timestamp",  # stg_recommendation_events physical col
            "session_start": "session_start",
            "period_month": "period_month",
            "payment_date": "payment_date",
            "signup_date": "signup_date",
        }

        primary_metric = intent.metrics[0] if intent.metrics else "mrr"

        # Fail loudly for unmapped metrics instead of silently defaulting to
        # fct_mrr_monthly / COUNT(*), which produced plausible-looking SQL that
        # crashed on Snowflake with a cryptic "invalid identifier" error.
        if primary_metric not in _METRIC_TABLE or primary_metric not in _METRIC_EXPR:
            hint = ""
            if primary_metric == "net_mrr_growth":
                hint = (
                    " It is a month-over-month derived (offset-window) metric that a flat "
                    "SELECT cannot express — use the dashboard net_mrr_growth widget."
                )
            raise SQLGenerationError(
                f"Metric '{primary_metric}' is not supported on the governed fallback path "
                f"(no table/expression mapping in the fallback builder)." + hint
            )

        table = _METRIC_TABLE[primary_metric]
        metric_expr = _METRIC_EXPR[primary_metric]

        # Translate semantic dimension names → physical column names for SELECT/GROUP BY
        physical_dims = [
            _DIM_COLUMN_MAP.get(_bare_dimension_name(d), _bare_dimension_name(d))
            for d in (intent.dimensions or [])
        ]
        select_parts = physical_dims[:]
        select_parts.append(metric_expr)

        where_clauses: list[str] = []

        # Apply user filters with bare physical column names. Without this, a
        # filtered query that falls back here would silently return UNFILTERED
        # numbers (e.g. worldwide revenue presented as "for country US").
        _FALLBACK_OP_MAP = {"eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
        for f in (intent.filters or []):
            col = _bare_dimension_name(f.column)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col):
                logger.warning("Fallback SQL: skipping filter on unsafe column name '%s'.", f.column)
                continue
            if f.operator == "in":
                vals = f.value if isinstance(f.value, list) else [f.value]
                val_str = ", ".join("'{}'".format(str(v).replace("'", "''")) for v in vals)
                where_clauses.append(f"{col} IN ({val_str})")
            else:
                op = _FALLBACK_OP_MAP.get(f.operator, "=")
                raw_val = str(f.value)
                try:
                    float(raw_val)
                    val_str = raw_val
                except ValueError:
                    val_str = "'{}'".format(raw_val.replace("'", "''"))
                where_clauses.append(f"{col} {op} {val_str}")

        if intent.time_range:
            # Physical time column, from the ONE map. This used to be a parallel
            # if/elif chain that had drifted from _METRIC_TIME_COL — both said
            # signup_date for churned_subscribers where the semantic layer says
            # churn_date, so a filtered churn count answered "who signed up in this
            # range and has since churned". One lookup, one place to be wrong.
            time_col = SQLGenerator._METRIC_TIME_COL.get(primary_metric, "")
            if not time_col:
                time_col = "payment_date"

            # Validate dates to prevent SQL injection (audit issue #1 / fallback path)
            _validate_date(intent.time_range.start_date, "start_date")
            _validate_date(intent.time_range.end_date, "end_date")
            where_clauses.append(
                f"{time_col} BETWEEN '{intent.time_range.start_date}' "
                f"AND '{intent.time_range.end_date}'"
            )

        # ── Subscriber-attribute join (Option B) ──────────────────────────────
        # fct_mrr_monthly carries no subscriber attributes (country, cohort_month,
        # acquisition_channel, churn_reason) — those live on dim_subscribers. When a
        # dimension or filter references one, wrap the fact in a derived table that
        # LEFT JOINs dim_subscribers so the outer SELECT/WHERE can use the bare
        # column name unambiguously. (The other marts carry country/plan_type
        # denormalized, so they never need this join.)
        _SUBSCRIBER_ONLY_DIMS = {
            "country", "cohort_month", "acquisition_channel",
            "churn_reason", "age_group", "subscription_status",
        }
        referenced_cols = set(physical_dims) | {
            _bare_dimension_name(f.column) for f in (intent.filters or [])
        }
        from_clause = table
        if table.endswith("fct_mrr_monthly"):
            join_cols = sorted(referenced_cols & _SUBSCRIBER_ONLY_DIMS)
            if join_cols:
                _sub_cols = ", ".join(f"sub.{c}" for c in join_cols)
                from_clause = (
                    "(\n"
                    f"        SELECT f.*, {_sub_cols}\n"
                    f"        FROM {table} f\n"
                    "        LEFT JOIN STREAMING_ANALYTICS.marts.dim_subscribers sub\n"
                    "          ON f.subscriber_id = sub.subscriber_id\n"
                    "    ) base"
                )

        group_by = ", ".join(
            str(i + 1) for i in range(len(physical_dims))
        ) if physical_dims else ""

        sql_parts = [
            "-- Gateway-governed SQL (MetricFlow fallback)",
            f"-- Generated for metrics: {', '.join(intent.metrics)}",
            "SELECT",
            "    " + ",\n    ".join(select_parts),
            f"FROM {from_clause}",
        ]

        if where_clauses:
            sql_parts.append("WHERE " + " AND ".join(where_clauses))

        if group_by:
            sql_parts.append(f"GROUP BY {group_by}")
            sql_parts.append(f"ORDER BY {group_by}")

        return "\n".join(sql_parts)
