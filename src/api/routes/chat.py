"""
Chat & GraphRAG Query API Router
Supports standard JSON query response and real-time SSE token-level streaming.
Fixes: P1-API-02, P1-API-03, P1-API-04, P3-CODE-04
"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from src.api.deps import get_current_user
from src.core.database import get_db
from src.models.db_models import User
from src.models.schemas import QueryRequest, QueryResponse
from src.services.rag_service import RAGService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/chat", tags=["Chat & RAG"])


@router.post("/query", response_model=QueryResponse)
async def query_rag(
    request: QueryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """5-Paradigm GraphRAG Question Answering Endpoint (naive, local, global, hybrid, mix)."""
    return await RAGService.query_hybrid(
        request=request,
        tenant_id=current_user.tenant_id,
        db=db,
        user_id=current_user.id,
    )


@router.post("/query/stream")
async def stream_query_rag(
    request: QueryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Native Token-Level Server-Sent Events (SSE) streaming endpoint."""
    stream_gen = await RAGService.query_stream(
        request=request,
        tenant_id=current_user.tenant_id,
        db=db,
        user_id=current_user.id,
    )
    return StreamingResponse(
        stream_gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
