"""
api/routes/metrics.py — GET /api/v1/metrics metric catalog endpoints.

Exposes the full certified metrics catalog.  No authentication required —
the catalog is read-only and contains no sensitive data.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from models.semantic import MetricDefinition

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Metrics Catalog"])


@router.get(
    "/metrics",
    response_model=list[MetricDefinition],
    summary="List all certified metrics",
    description=(
        "Returns the full catalog of certified metrics including their "
        "descriptions, grains, certified dimensions, and lineage. "
        "Only metrics that have passed semantic governance certification appear here."
    ),
)
async def list_metrics(request: Request) -> list[MetricDefinition]:
    """Return all certified metrics from the registry."""
    registry = request.app.state.metric_registry
    metrics = registry.list_metrics()
    logger.debug("Returning %d certified metrics.", len(metrics))
    return metrics


@router.get(
    "/metrics/{metric_name}",
    response_model=MetricDefinition,
    summary="Get a single certified metric",
    description="Returns full detail for a single certified metric by name.",
)
async def get_metric(metric_name: str, request: Request) -> MetricDefinition:
    """
    Return a single MetricDefinition by name.

    Args:
        metric_name: The metric name (case-insensitive, e.g. ``mrr``).

    Raises:
        HTTPException 404: If the metric is not in the certified registry.
    """
    registry = request.app.state.metric_registry
    metric = registry.get_metric(metric_name)
    if metric is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Metric '{metric_name}' is not a certified metric. "
                f"Available metrics: {[m.name for m in registry.list_metrics()]}."
            ),
        )
    return metric
