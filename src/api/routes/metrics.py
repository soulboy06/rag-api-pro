"""
Prometheus Metrics Route
Exports Prometheus-formatted metrics at /metrics.
"""
from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST
from src.core.monitoring.metrics import prometheus_metrics

router = APIRouter(tags=["Observability"])


@router.get("/metrics")
async def get_prometheus_metrics():
    """Prometheus scrape endpoint."""
    metrics_data = prometheus_metrics.export_metrics()
    return Response(
        content=metrics_data,
        media_type=CONTENT_TYPE_LATEST
    )
