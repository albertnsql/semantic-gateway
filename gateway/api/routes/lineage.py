"""
api/routes/lineage.py — GET /api/v1/lineage/{metric_name} endpoint.

Returns the full upstream lineage trace for a certified metric, showing
every transformation step from raw source tables to the certified mart.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from core.exceptions import MetricNotFoundError
from core.lineage_resolver import LineageTrace

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Lineage"])


@router.get(
    "/lineage/{metric_name}",
    response_model=LineageTrace,
    summary="Get lineage trace for a metric",
    description=(
        "Returns the full upstream lineage trace for a certified metric. "
        "Shows the transformation path from raw source tables through staging, "
        "intermediate, and mart layers to the certified metric definition."
    ),
)
async def get_lineage(metric_name: str, request: Request) -> LineageTrace:
    """
    Resolve and return full lineage for a metric.

    Args:
        metric_name: Certified metric name (case-insensitive).

    Raises:
        HTTPException 404: If the metric is not in the certified registry.
    """
    resolver = request.app.state.lineage_resolver

    try:
        trace = resolver.resolve_metric(metric_name)
    except MetricNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error resolving lineage for '%s'.", metric_name)
        raise HTTPException(
            status_code=500,
            detail=f"Lineage resolution failed: {exc}",
        ) from exc

    logger.info(
        "Lineage resolved for metric='%s': %d steps.",
        metric_name,
        len(trace.transformation_steps),
    )
    return trace
