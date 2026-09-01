"""
Level 7: Enterprise Request-ID Tracking, Audit Logging & Global Sanitized Error Middleware
Injects tracing headers, records structured audit logs to PostgreSQL, and formats sanitized error responses.
Fixes: P1-API-05, P2-OBS-05, P2-OBS-08, P3-CODE-02
"""
import time
import uuid
import asyncio
from typing import Optional
from datetime import datetime, timezone
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.core.logger import request_id_ctx, tenant_id_ctx, user_id_ctx, get_logger
from src.core.database import AsyncSessionLocal
from src.models.db_models import AuditLog, utcnow
from src.core.exceptions import RAGException
from src.core.monitoring.metrics import prometheus_metrics

logger = get_logger("rag.api.gateway")


async def _persist_audit_log(
    tenant_id: Optional[str],
    user_id: Optional[str],
    request_id: str,
    endpoint: str,
    method: str,
    status_code: int,
    latency_ms: float,
    client_ip: Optional[str]
) -> None:
    """Asynchronously writes structured audit log entry into PostgreSQL."""
    try:
        t_id = tenant_id if tenant_id and tenant_id != "-" else None
        u_id = user_id if user_id and user_id != "-" else None
        async with AsyncSessionLocal() as db:
            log_entry = AuditLog(
                tenant_id=t_id,
                user_id=u_id,
                request_id=request_id,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                latency_ms=latency_ms,
                client_ip=client_ip,
                created_at=utcnow()
            )
            db.add(log_entry)
            await db.commit()
    except Exception as exc:
        logger.debug(f"Audit log persistence skipped/failed: {exc}")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # 1. Extract or generate unique Request-ID
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request_id_ctx.set(req_id)
        
        # Reset tenant & user contexts
        tenant_id_ctx.set("-")
        user_id_ctx.set("-")

        start_time = time.perf_counter()
        client_ip = request.client.host if request.client else "-"
        
        # 2. Process Request
        try:
            response = await call_next(request)
        except Exception as e:
            duration_s = time.perf_counter() - start_time
            latency_ms = round(duration_s * 1000, 2)
            t_id = tenant_id_ctx.get()
            u_id = user_id_ctx.get()
            prometheus_metrics.record_request(
                tenant_id=t_id,
                endpoint=request.url.path,
                status_code=500,
                duration_seconds=duration_s
            )
            logger.error(
                f"Unhandled HTTP error on {request.method} {request.url.path}: {str(e)}",
                extra={"latency_ms": latency_ms, "status_code": 500}
            )
            # Async audit log persistence
            asyncio.create_task(
                _persist_audit_log(
                    tenant_id=t_id,
                    user_id=u_id,
                    request_id=req_id,
                    endpoint=request.url.path,
                    method=request.method,
                    status_code=500,
                    latency_ms=latency_ms,
                    client_ip=client_ip
                )
            )
            # Sanitized error response
            return JSONResponse(
                status_code=500,
                content={
                    "error_code": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected server error occurred. Please contact system administrator.",
                    "request_id": req_id,
                    "timestamp": utcnow().isoformat()
                },
                headers={"X-Request-ID": req_id}
            )

        duration_s = time.perf_counter() - start_time
        latency_ms = round(duration_s * 1000, 2)
        response.headers["X-Request-ID"] = req_id
        t_id = getattr(request.state, "tenant_id", None) or tenant_id_ctx.get()
        u_id = getattr(request.state, "user_id", None) or user_id_ctx.get()

        # Record Prometheus RED metrics
        prometheus_metrics.record_request(
            tenant_id=t_id,
            endpoint=request.url.path,
            status_code=response.status_code,
            duration_seconds=duration_s
        )

        # 3. Structured Logging
        logger.info(
            f"{request.method} {request.url.path} -> {response.status_code} ({latency_ms}ms)",
            extra={
                "endpoint": request.url.path,
                "method": request.method,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "client_ip": client_ip,
            }
        )

        # 4. Async Audit Log DB Persistence (Skip pure health/metrics scraping if needed, but persist API calls)
        if not request.url.path.startswith("/health") and not request.url.path.startswith("/metrics"):
            asyncio.create_task(
                _persist_audit_log(
                    tenant_id=t_id,
                    user_id=u_id,
                    request_id=req_id,
                    endpoint=request.url.path,
                    method=request.method,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    client_ip=client_ip
                )
            )

        return response
