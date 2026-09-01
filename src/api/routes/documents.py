"""
Level 1: Documents API Router
Supports upload with magic number inspection, document listing, presigned URL generation, and safe deletion.
Fixes: P0-SEC-02, P0-SEC-03, P1-API-07, P1-API-08, P1-STORE-04
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.api.deps import get_current_user, require_role
from src.core.security.rbac import Role
from src.models.db_models import User
from src.models.schemas import DocumentInfo, UploadResponse
from src.services.doc_service import DocService

router = APIRouter(prefix="/documents", tags=["Documents"])


@router.post("/upload", response_model=UploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    kb_id: str = Form(...),
    current_user: User = Depends(require_role(Role.MEMBER)),
    db: AsyncSession = Depends(get_db)
):
    """Uploads document with stream size truncation, magic number verification, and PG/MinIO registration."""
    return await DocService.upload_document(
        db=db,
        file=file,
        tenant_id=current_user.tenant_id,
        kb_id=kb_id,
    )


@router.get("", response_model=List[DocumentInfo])
async def list_documents(
    kb_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Lists all documents within tenant/knowledge base."""
    return await DocService.list_documents(
        db=db,
        tenant_id=current_user.tenant_id,
        kb_id=kb_id,
    )


@router.get("/{doc_id}/download-url")
async def get_document_download_url(
    doc_id: str,
    expires_seconds: int = Query(3600, ge=60, le=86400),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Generates temporary presigned download URL from MinIO."""
    url = await DocService.get_download_url(
        db=db,
        doc_id=doc_id,
        tenant_id=current_user.tenant_id,
        expires_seconds=expires_seconds,
    )
    return {"doc_id": doc_id, "download_url": url, "expires_seconds": expires_seconds}


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: str,
    current_user: User = Depends(require_role(Role.MEMBER)),
    db: AsyncSession = Depends(get_db)
):
    """Safely deletes document from PostgreSQL metadata, MinIO, Qdrant and Memgraph."""
    deleted = await DocService.delete_document(
        db=db,
        doc_id=doc_id,
        tenant_id=current_user.tenant_id,
    )
    return {"doc_id": doc_id, "deleted": deleted, "message": "Document deleted successfully"}


@router.post("/{doc_id}/reingest", response_model=dict)
async def reingest_document(
    doc_id: str,
    force_parser: Optional[str] = Query(None, description="Optional override parser name"),
    current_user: User = Depends(require_role(Role.MEMBER)),
    db: AsyncSession = Depends(get_db)
):
    """Triggers re-ingestion task for an existing document."""
    task_info = await DocService.reingest_document(
        db=db,
        doc_id=doc_id,
        tenant_id=current_user.tenant_id,
        force_parser=force_parser
    )
    return {
        "doc_id": doc_id,
        "task_id": task_info.id,
        "status": task_info.status,
        "message": "Re-ingestion task successfully enqueued"
    }

