import asyncio
import logging

from config import settings
from core.manifest_parser import ManifestParser
from core.metric_registry import MetricRegistry
from core.semantic_validator import SemanticValidator
from core.sql_generator import SQLGenerator
from core.intent_extractor import QueryIntent
from core.sql_generator import build_dimension_prefix_map

logging.basicConfig(level=logging.INFO)

async def test_validation():
    # Load registry
    parser = ManifestParser()
    parser.load(settings.manifest_path)
    
    registry = MetricRegistry()
    registry.load(settings.metrics_path, settings.semantic_models_path, parser)
    
    # Run map build
    build_dimension_prefix_map()
    
    validator = SemanticValidator(registry)
    
    tests = [
        {
            "query": "LTV by plan type",
            "metrics": ["ltv"],
            "dimensions": ["subscriber__plan_type"]
        },
        {
            "query": "Engagement rate by content type",
            "metrics": ["engagement_rate"],
            "dimensions": ["content__content_type"]
        },
        {
            "query": "Total subscribers by plan type",
            "metrics": ["total_subscribers"],
            "dimensions": ["subscriber__plan_type"]
        }
    ]
    
    for t in tests:
        intent = QueryIntent(
            query=t["query"],
            original_query=t["query"],
            metrics=t["metrics"],
            dimensions=t["dimensions"]
        )
        result = validator.validate(intent)
        print(f"\nQuery: {t['query']}")
        print(f"Safe to execute: {result.safe_to_execute}")
        if not result.safe_to_execute:
            print(f"Violations: {result.violations}")

if __name__ == "__main__":
    asyncio.run(test_validation())
