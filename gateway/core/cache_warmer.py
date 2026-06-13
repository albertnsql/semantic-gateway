"""
cache_warmer.py — Background Cache Warmer for AI Semantic Gateway.

Pre-computes MetricFlow queries and Snowflake results at startup 
so that common queries are instantaneous for users.
"""

import logging
import threading
import time

from config import Settings
from cache import QueryCache
from core.sql_generator import SQLGenerator
from core.response_builder import ResponseBuilder
from core.semantic_validator import SemanticValidator, ValidationResult
from core.intent_extractor import QueryIntent

logger = logging.getLogger(__name__)


class CacheWarmer:
    """
    Runs in a background thread at startup. Iterates through the WARMUP_MATRIX
    and executes the full query pipeline, caching the results in the QueryCache.
    """

    def __init__(
        self,
        settings: Settings,
        sql_generator: SQLGenerator,
        query_cache: QueryCache,
        response_builder: ResponseBuilder,
    ) -> None:
        self.settings = settings
        self.sql_gen = sql_generator
        self.query_cache = query_cache
        self.response_builder = response_builder
        self._stop_event = threading.Event()
        self._thread = None

    def start(self) -> None:
        """Start the background warm-up thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the warmer to stop and wait for it."""
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=2.0)

    def _intent_to_dict(self, intent: QueryIntent) -> dict:
        """Serialize QueryIntent same as in query.py"""
        return intent.model_dump(mode="json", exclude={"raw_llm_response", "original_query"})

    def _run(self) -> None:
        """Background loop executing the warm-up matrix."""
        matrix = self.settings.warmup_matrix
        total_combinations = sum(len(dims) for dims in matrix.values())
        
        logger.info("[CacheWarmer] Starting warm-up: %d combinations queued", total_combinations)
        start_time_all = time.perf_counter()
        
        warmed = 0
        skipped = 0
        failed = 0

        for metric, dimensions in matrix.items():
            for dim in dimensions:
                if self._stop_event.is_set():
                    logger.info("[CacheWarmer] Warm-up cancelled by shutdown.")
                    return
                
                # Construct intent
                intent = QueryIntent(
                    original_query=f"Show {metric} by {dim}",
                    metrics=[metric],
                    dimensions=[dim],
                    time_range=None,
                    aggregation_level=None,
                    filters=[]
                )
                
                intent_dict = self._intent_to_dict(intent)
                
                # Check if already cached
                if self.query_cache.get(intent_dict) is not None:
                    skipped += 1
                    logger.info("[CacheWarmer] SKIP %s × %s — already cached", metric, dim)
                    continue

                # Execute pipeline
                start_time_single = time.perf_counter()
                try:
                    # 2. SQL Generation (bypass semantic validation as WARMUP_MATRIX is pre-certified)
                    gen_query = self.sql_gen.generate(intent, None)
                    
                    # 3. Snowflake execution (max 500 rows to match UI default)
                    all_rows = self.sql_gen.execute_query(gen_query.compiled_sql)
                    results = all_rows[:500]
                    
                    # 4. Build response payload directly (bypassing response_builder)
                    payload = {
                        "request_id": "warmup",
                        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                        "status": "success",
                        "query": {
                            "original": intent.original_query,
                            "interpreted_metrics": intent.metrics,
                            "interpreted_dimensions": intent.dimensions,
                            "time_range": None
                        },
                        "validation": {
                            "passed": True,
                            "checks_run": [],
                            "violations": []
                        },
                        "governance": {
                            "certified_definition": "",
                            "grain": "",
                            "grain_columns": [],
                            "lineage_path": [],
                            "source_model": "",
                            "warning": None
                        },
                        "result": {
                            "row_count": len(results),
                            "data": results,
                            "generated_sql": gen_query.compiled_sql,
                            "metricflow_query": gen_query.metricflow_query
                        },
                        "rejection": {
                            "reason": None,
                            "violated_rules": [],
                            "suggested_fix": None,
                            "safe_alternatives": []
                        },
                        "cache_hit": False,
                        "narrative_summary": ""
                    }
                    
                    # 4b. Generate narrative summary so frontend gets full data on cache hits
                    try:
                        from api.routes.query import _generate_narrative
                        payload["narrative_summary"] = _generate_narrative(
                            intent.original_query, results, intent, self.settings
                        )
                    except Exception as narr_exc:
                        logger.warning("[CacheWarmer] Failed to generate narrative: %s", narr_exc)
                    
                    # 5. Cache result
                    self.query_cache.set(intent_dict, payload)
                    
                    elapsed = time.perf_counter() - start_time_single
                    warmed += 1
                    logger.info("[CacheWarmer] Warmed %s × %s (%.1fs elapsed)", metric, dim, elapsed)
                    
                except Exception as exc:
                    failed += 1
                    logger.error("[CacheWarmer] FAILED %s × %s — %s", metric, dim, exc)

        total_elapsed = time.perf_counter() - start_time_all
        logger.info(
            "[CacheWarmer] Warm-up complete: %d warmed, %d skipped, %d failed in %.1fs",
            warmed, skipped, failed, total_elapsed
        )
