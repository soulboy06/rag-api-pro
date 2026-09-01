"""
Knowledge Base Management API Router
Provides tenant-isolated CRUD for knowledge bases.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from src.core.database import get_db
from src.api.deps import get_current_user, require_role
from src.core.security.rbac import Role
from src.models.db_models import KnowledgeBase, User, Document
from src.core.exceptions import ResourceNotFoundError

router = APIRouter(prefix="/knowledge-bases", tags=["Knowledge Bases"])


class CreateKBRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None


class KBInfo(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: Optional[str]
    created_at: str


@router.post("", response_model=KBInfo, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    request: CreateKBRequest,
    current_user: User = Depends(require_role(Role.TENANT_ADMIN)),
    db: AsyncSession = Depends(get_db)
):
    """Creates a new knowledge base for the active tenant."""
    kb = KnowledgeBase(
        tenant_id=current_user.tenant_id,
        name=request.name,
        description=request.description
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return KBInfo(
        id=kb.id,
        tenant_id=kb.tenant_id,
        name=kb.name,
        description=kb.description,
        created_at=kb.created_at.isoformat()
    )


@router.get("", response_model=List[KBInfo])
async def list_knowledge_bases(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Lists knowledge bases belonging strictly to the current tenant."""
    stmt = select(KnowledgeBase).where(KnowledgeBase.tenant_id == current_user.tenant_id)
    res = await db.execute(stmt)
    kbs = res.scalars().all()
    return [
        KBInfo(
            id=k.id,
            tenant_id=k.tenant_id,
            name=k.name,
            description=k.description,
            created_at=k.created_at.isoformat()
        )
        for k in kbs
    ]


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: str,
    current_user: User = Depends(require_role(Role.TENANT_ADMIN)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes a knowledge base with tenant isolation."""
    stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.tenant_id == current_user.tenant_id
    )
    res = await db.execute(stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise ResourceNotFoundError(f"Knowledge base '{kb_id}' not found")

    await db.delete(kb)
    await db.commit()
