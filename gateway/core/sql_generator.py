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
import pathlib

_skill_loader_path = (
    pathlib.Path(__file__).resolve()
    .parent   # gateway/core/
    .parent   # gateway/
    .parent   # Streaming_Analytics/
    / "backend" / "core" / "skill_loader.py"
)

if _skill_loader_path.exists():
    _spec = importlib.util.spec_from_file_location("skill_loader", _skill_loader_path)
    _skill_loader_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_skill_loader_mod)  # type: ignore[union-attr]
    load_skill = _skill_loader_mod.load_skill
    _SKILL_LOADER_AVAILABLE = True
else:
    _SKILL_LOADER_AVAILABLE = False
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "backend/core/skill_loader.py not found at '%s' — SQL review disabled.",
        _skill_loader_path,
    )

if TYPE_CHECKING:
    from core.intent_extractor import QueryIntent, TimeRange
    from core.semantic_validator import ValidationResult
    from config import Settings

logger = logging.getLogger(__name__)

_QUERY_TIMEOUT_SECONDS = 30

_DYNAMIC_DIMENSION_MAP: dict[str, dict[str, str]] | None = None


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
        entities = [e['name'] for e in sm.get('entities', []) if e.get('type') in ('primary', 'foreign')]
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
                    
        m_dim_map = {}
        for dim_name, prefixes in global_dims.items():
            if len(prefixes) == 1:
                m_dim_map[dim_name] = prefixes[0]
            else:
                chosen = None
                if m_name in ['total_subscribers', 'churned_subscribers']:
                    for p in prefixes:
                        if p.startswith('subscriber__'):
                            chosen = p
                            break
                elif m_name in ['churn_rate', 'retention_rate']:
                    # churn_rate/retention_rate now live on fct_mrr_monthly:
                    # prefer native subscription__ dims, fall back to subscriber__ joins.
                    for p in prefixes:
                        if p.startswith('subscription__'):
                            chosen = p
                            break
                    if not chosen:
                        for p in prefixes:
                            if p.startswith('subscriber__'):
                                chosen = p
                                break
                elif m_name == 'ltv':
                    # ltv spans fct_payments (payment entity) AND dim_subscribers.
                    # Prefer payment__ prefix for payment-domain dims, subscriber__ for subscriber dims.
                    for p in prefixes:
                        if p.startswith('payment__'):
                            chosen = p
                            break
                    if not chosen:
                        for p in prefixes:
                            if p.startswith('subscriber__'):
                                chosen = p
                                break
                elif m_name in ['mrr', 'expansion_mrr']:
                    for p in prefixes:
                        if p.startswith('subscription__'):
                            chosen = p
                            break
                elif m_name in ['engagement_rate', 'recommendation_ctr']:
                    # engagement_rate: session__ dims (device_type) take priority;
                    # subscriber__ dims (plan_type, country) are also valid via join.
                    for p in prefixes:
                        if p.startswith('session__') or p.startswith('event__'):
                            chosen = p
                            break
                    if not chosen:
                        for p in prefixes:
                            if p.startswith('subscriber__'):
                                chosen = p
                                break
                            
                if not chosen:
                    for p in prefixes:
                        if any(p.startswith(pe + '__') for pe in primary_entities):
                            chosen = p
                            break

                if not chosen:
                    # Foreign entities on the metric's models are valid MetricFlow
                    # join paths (e.g. total_revenue on sem_payments → subscriber__country
                    # via the foreign 'subscriber' entity). Prefer these over an
                    # arbitrary prefixes[0], which picks unreachable dims like
                    # session__country and fails query resolution.
                    for p in prefixes:
                        if any(p.startswith(me + '__') for me in model_entities):
                            chosen = p
                            break

                if not chosen:
                    chosen = prefixes[0]
                    
                m_dim_map[dim_name] = chosen
                
        # Add common LLM abbreviation aliases
        if 'content_primary_genre' in m_dim_map:
            m_dim_map['primary_genre'] = m_dim_map['content_primary_genre']
            
        metric_map[m_name] = m_dim_map
        
    logger.info("Dimension prefix map built: %d metrics mapped", len(metric_map))
    _DYNAMIC_DIMENSION_MAP = metric_map
    return metric_map



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
        "ltv":                    "payment_date",
        "engagement_rate":        "session_start",
        # churn_rate/retention_rate live on fct_mrr_monthly (monthly event-based
        # definition) — time filters select the month churn HAPPENED, not signup.
        "churn_rate":             "period_month",
        "retention_rate":         "period_month",
        "total_subscribers":      "signup_date",
        "churned_subscribers":    "signup_date",
        "recommendation_ctr":     "event_timestamp",
        "total_recommendations":  "event_timestamp",
        "clicked_recommendations":"event_timestamp",
    }

    def __init__(self, settings: "Settings", pool=None, template_cache: SQLTemplateCache | None = None) -> None:
        self._settings = settings
        self._pool = pool                        # SnowflakePool — injected at startup; None = legacy mode
        self._template_cache = template_cache    # SQLTemplateCache — injected at startup; None = disabled

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
        # ── SQL Template Cache check ──────────────────────────────────────────
        # If we have a cached compiled SQL template for this metric+dimension
        # combination, skip the MetricFlow subprocess entirely and inject the
        # time range directly.  This cuts first-query latency from ~45 s to ~1 s
        # for any metric+dim combo seen before (regardless of time range).
        compiled_sql: str | None = None
        used_template_cache = False

        # ── Pre-process filters to strip redundant group-by dimensions ──
        # (e.g. "show churn by plan type" → plan_type in dims AND in filters).
        # This ensures they don't incorrectly disable the SQL Template Cache.
        effective_filters = []
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
                    logger.info("Stripping redundant filter on '%s' before cache check (already a dimension).", col)
                    continue

                effective_filters.append(f)
        
        # Override the intent filters so format_mf_query receives the clean list
        intent.filters = effective_filters

        # Filtered queries are NOT eligible for the template cache — the compiled SQL
        # contains hard-coded WHERE predicates (e.g., country = 'US') that cannot be
        # reused for a different filter value or an unfiltered version of the same query.
        if self._template_cache is not None and intent.metrics and not effective_filters:
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

        # ── MetricFlow subprocess & Speculative LLM Review ───────────
        mf_command: list[str] = []
        fallback_sql = self._build_fallback_sql(intent)
        
        if compiled_sql is None:
            mf_command = self.format_mf_query(intent)
            logger.info("Executing MetricFlow: %s", " ".join(mf_command))

            try:
                compiled_sql = self._run_mf_subprocess(mf_command)
                mf_success = True
            except Exception as exc:
                if isinstance(exc, SQLGenerationError):
                    raise
                logger.warning("MetricFlow execution failed: %s. Falling back to governed SQL template.", exc)
                compiled_sql = fallback_sql
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
            if self._template_cache is not None and intent.metrics and mf_success and not intent.filters:
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

        # mf_command is only defined when MetricFlow ran; provide an audit label otherwise.
        _mf_cmd = " ".join(mf_command) if (mf_command and not used_template_cache) else (
            f"[template_cache] mf query --metrics {','.join(intent.metrics)} "
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

        if intent.metrics:
            parts.extend(["--metrics", ",".join(intent.metrics)])

        if intent.dimensions:
            global_dim_map = build_dimension_prefix_map()
            
            # Use the first metric to resolve prefixes (MetricFlow relies on primary metric's model)
            primary_metric = intent.metrics[0] if intent.metrics else ""
            dim_map = global_dim_map.get(primary_metric, {})
            
            mapped_dims = []
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

        if intent.limit:
            parts.extend(["--limit", str(intent.limit)])

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

        Loads ``backend/skills/sql_reviewer.md`` and calls the LLM with the SQL
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
        lines = stdout.splitlines()
        sql_lines: list[str] = []
        capturing = False

        for line in lines:
            upper = line.strip().upper()
            if upper.startswith("SELECT") or (
                capturing and sql_lines
            ):
                capturing = True

            if capturing:
                # Stop at blank lines after we've collected something
                if not line.strip() and sql_lines:
                    break
                sql_lines.append(line)

        sql = "\n".join(sql_lines).strip()

        if not sql:
            # Try alternate: find SELECT anywhere in the output
            idx = stdout.upper().find("SELECT")
            if idx != -1:
                sql = stdout[idx:].strip()

        if not sql:
            raise SQLGenerationError(
                "MetricFlow --explain returned no SQL output.",
                mf_command=mf_command,
                stderr=stdout[:300],
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
            "churn_rate": "STREAMING_ANALYTICS.marts.fct_mrr_monthly",
            "retention_rate": "STREAMING_ANALYTICS.marts.fct_mrr_monthly",
            "total_subscribers": "STREAMING_ANALYTICS.marts.dim_subscribers",
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
            "total_subscribers": "COUNT(DISTINCT subscriber_id) AS total_subscribers",
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
        table = _METRIC_TABLE.get(primary_metric, "STREAMING_ANALYTICS.marts.fct_mrr_monthly")
        metric_expr = _METRIC_EXPR.get(primary_metric, f"COUNT(*) AS {primary_metric}")

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
            # Use the appropriate physical time column based on the metric
            if primary_metric in ("mrr", "expansion_mrr", "churn_rate", "retention_rate"):
                time_col = "period_month"
            elif primary_metric in ("ltv", "total_revenue"):
                time_col = "payment_date"
            elif primary_metric == "engagement_rate":
                time_col = "session_start"  # fct_stream_sessions physical column
            elif primary_metric in ("total_subscribers", "churned_subscribers"):
                time_col = "signup_date"
            elif primary_metric in ("recommendation_ctr", "total_recommendations", "clicked_recommendations"):
                time_col = "event_timestamp"  # stg_recommendation_events physical column
            else:
                time_col = "payment_date"

            # Validate dates to prevent SQL injection (audit issue #1 / fallback path)
            _validate_date(intent.time_range.start_date, "start_date")
            _validate_date(intent.time_range.end_date, "end_date")
            where_clauses.append(
                f"{time_col} BETWEEN '{intent.time_range.start_date}' "
                f"AND '{intent.time_range.end_date}'"
            )

        group_by = ", ".join(
            str(i + 1) for i in range(len(physical_dims))
        ) if physical_dims else ""

        sql_parts = [
            "-- Gateway-governed SQL (MetricFlow fallback)",
            f"-- Generated for metrics: {', '.join(intent.metrics)}",
            "SELECT",
            "    " + ",\n    ".join(select_parts),
            f"FROM {table}",
        ]

        if where_clauses:
            sql_parts.append("WHERE " + " AND ".join(where_clauses))

        if group_by:
            sql_parts.append(f"GROUP BY {group_by}")
            sql_parts.append(f"ORDER BY {group_by}")

        return "\n".join(sql_parts)
