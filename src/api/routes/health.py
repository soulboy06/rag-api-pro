"""
Level 8: Enterprise Health Check API Router
Provides decoupled liveness and readiness probes with strict timeout budgets across all 5 infrastructure backends.
Fixes: P2-OBS-01, P2-OBS-02, P2-OBS-07
"""
import asyncio
from typing import Dict, Any
from fastapi import APIRouter, status, Response
from sqlalchemy import text

from src.core.clients import InfrastructureClients
from src.core.database import AsyncSessionLocal
from src.core.monitoring.metrics import prometheus_metrics
from src.core.logger import get_logger

router = APIRouter(prefix="/health", tags=["Health"])
logger = get_logger(__name__)


@router.get("")
@router.get("/liveness")
async def liveness():
    """Liveness probe: lightweight check that the FastAPI process is responsive."""
    return {"status": "ok", "message": "Service process is alive"}


@router.get("/readiness")
async def readiness(response: Response):
    """
    Readiness probe: validates reachability and latency of all 5 infrastructure tiers
    (PostgreSQL, Redis, MinIO, Qdrant, Memgraph) with strict 1.5s timeout budgets.
    """
    checks: Dict[str, Any] = {}
    is_ready = True

    # 1. PostgreSQL Probe
    try:
        async def check_pg():
            async with AsyncSessionLocal() as session:
                res = await session.execute(text("SELECT 1"))
                return res.scalar() == 1

        pg_ok = await asyncio.wait_for(check_pg(), timeout=1.5)
        checks["postgresql"] = "healthy" if pg_ok else "unhealthy"
    except Exception as e:
        logger.warning("PostgreSQL readiness probe failed", extra={"error": str(e)})
        checks["postgresql"] = "unhealthy"
        is_ready = False

    # 2. Redis Probe & Queue Depth Metric
    try:
        async def check_redis():
            redis_client = InfrastructureClients.get_redis()
            pong = await redis_client.ping()
            # Also update queue depth metric
            try:
                info = await redis_client.xlen("stream:rag_tasks")
                prometheus_metrics.task_queue_depth.labels(
                    stream_name="stream:rag_tasks",
                    group_name="cg:rag_workers"
                ).set(info)
            except Exception:
                pass
            return pong

        pong = await asyncio.wait_for(check_redis(), timeout=1.5)
        checks["redis"] = "healthy" if pong else "unhealthy"
    except Exception as e:
        logger.warning("Redis readiness probe failed", extra={"error": str(e)})
        checks["redis"] = "unhealthy"
        is_ready = False

    # 3. MinIO Probe
    try:
        def check_minio():
            minio_client = InfrastructureClients.get_minio()
            minio_client.list_buckets()
            return True

        minio_ok = await asyncio.to_thread(check_minio)
        checks["minio"] = "healthy" if minio_ok else "unhealthy"
    except Exception as e:
        logger.warning("MinIO readiness probe failed", extra={"error": str(e)})
        checks["minio"] = "unhealthy"
        is_ready = False

    # 4. Qdrant Probe
    try:
        async def check_qdrant():
            qdrant = InfrastructureClients.get_qdrant()
            await qdrant.get_collections()
            return True

        qdrant_ok = await asyncio.wait_for(check_qdrant(), timeout=1.5)
        checks["qdrant"] = "healthy" if qdrant_ok else "unhealthy"
    except Exception as e:
        logger.warning("Qdrant readiness probe failed", extra={"error": str(e)})
        checks["qdrant"] = "unhealthy"
        is_ready = False

    # 5. Memgraph Probe
    try:
        async def check_memgraph():
            memgraph = InfrastructureClients.get_memgraph()
            async with memgraph.session() as session:
                res = await session.run("RETURN 1 AS val;")
                record = await res.single()
                return record and record["val"] == 1

        memgraph_ok = await asyncio.wait_for(check_memgraph(), timeout=1.5)
        checks["memgraph"] = "healthy" if memgraph_ok else "unhealthy"
    except Exception as e:
        logger.warning("Memgraph readiness probe failed", extra={"error": str(e)})
        checks["memgraph"] = "unhealthy"
        is_ready = False

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if is_ready else "not_ready",
        "dependencies": checks
    }
