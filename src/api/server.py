"""
Level 0: FastAPI Server Entrypoint & Application Factory
Configures structured logging, CORS, RequestContext middleware, and centralized error sanitization.
Fixes: P1-API-05, P2-OBS-04, P3-CODE-02
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from src.core.config import settings
from src.core.logger import setup_logging, get_logger, request_id_ctx
from src.core.exceptions import RAGException
from src.core.clients import InfrastructureClients
from src.core.database import init_db
from src.api.middleware import RequestContextMiddleware
from src.api.routes import auth, documents, tasks, chat, health, knowledge_bases, tenants, metrics

logger = get_logger("rag.api.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup structured logging
    setup_logging(
        log_level=settings.LOG_LEVEL,
        json_format=settings.observability.ENABLE_JSON_LOGS
    )
    logger.info("Initializing infrastructure components (Buckets, collections, groups)...")
    await init_db()
    await InfrastructureClients.init_infrastructure()
    logger.info("Infrastructure successfully initialized.")
    yield
    # Shutdown: Close all connections cleanly
    logger.info("Shutting down and closing all external connections...")
    await InfrastructureClients.close_all()
    logger.info("All connections closed.")


app = FastAPI(
    title="Multi-Tenant Multimodal GraphRAG API",
    version="0.1.0",
    description="Enterprise Multi-Tenant GraphRAG System (Level 0 Foundation & Models)",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Request-ID & Observability Middleware (Must be before CORS)
app.add_middleware(RequestContextMiddleware)

# CORS Middleware - Enforces safe origin policies
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.CORS_ALLOWED_ORIGINS.split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Centralized Exception Handler for RAG domain exceptions
@app.exception_handler(RAGException)
async def rag_exception_handler(request: Request, exc: RAGException):
    req_id = request_id_ctx.get()
    logger.warning(
        f"Domain exception [{exc.error_code}]: {exc.message}",
        extra={"error_code": exc.error_code, "status_code": exc.status_code, "details": exc.details}
    )
    return JSONResponse(
        status_code=exc.status_code,
        headers={"X-Request-ID": req_id},
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            "request_id": req_id,
            "details": exc.details,
        }
    )


# General Exception Handler (sanitizes unexpected internal exceptions)
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    req_id = request_id_ctx.get()
    logger.error(
        f"Unhandled system error: {str(exc)}",
        exc_info=True,
        extra={"request_id": req_id, "status_code": 500}
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        headers={"X-Request-ID": req_id},
        content={
            "error_code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected internal server error occurred",
            "request_id": req_id,
        }
    )


# Include Routers
app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(knowledge_bases.router, prefix="/api/v1")
app.include_router(tenants.router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
