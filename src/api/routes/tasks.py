"""
Tasks API Router
Provides task query, cursor pagination, and manual retry endpoints.
"""
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.api.deps import get_current_user
from src.models.db_models import User
from src.models.schemas import TaskInfo
from src.services.task_service import TaskService

router = APIRouter(prefix="/tasks", tags=["Tasks"])


@router.get("", response_model=List[TaskInfo])
async def list_tasks(
    kb_id: Optional[str] = Query(None, description="Filter tasks by knowledge base"),
    cursor: Optional[datetime] = Query(None, description="Cursor timestamp for pagination"),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Lists ingestion tasks for the current tenant with cursor-based pagination."""
    return await TaskService.list_tasks_cursor(
        db=db,
        tenant_id=current_user.tenant_id,
        kb_id=kb_id,
        cursor_created_at=cursor,
        limit=limit
    )


@router.get("/{task_id}", response_model=TaskInfo)
async def get_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Fetches real-time status and details of an ingestion task."""
    return await TaskService.get_task(
        db=db,
        task_id=task_id,
        tenant_id=current_user.tenant_id
    )


@router.post("/{task_id}/retry", response_model=TaskInfo)
async def retry_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Manually re-enqueues a failed or dead-letter task."""
    return await TaskService.retry_task(
        db=db,
        task_id=task_id,
        tenant_id=current_user.tenant_id
    )
