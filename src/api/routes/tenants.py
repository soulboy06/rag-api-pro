"""
Tenant Configuration & Hot Update API Router
Integrates Level 4 TenantConfigManager with versioned snapshots and atomic rollback.
Fixes: P1-API-10, P1-CORE-08..12
"""
from typing import Dict, Any, Optional, Literal
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from src.api.deps import get_current_user, require_role
from src.core.database import get_db
from src.core.security.rbac import Role
from src.models.db_models import User, KnowledgeBase
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.tenant.config_manager import TenantConfigManager, TenantRuntimeConfig
from src.models.db_models import Tenant
from src.core.security import get_password_hash
from src.core.exceptions import AuthorizationError, ValidationError as DomainValidationError
from src.core.tenant.prompts import TenantPromptManager

router = APIRouter(prefix="/tenants", tags=["Tenant Settings"])


class TenantInfo(BaseModel):
    id: str
    name: str
    created_at: str


class CreateTenantRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class CreateTenantUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)
    role: Literal["TENANT_ADMIN", "MEMBER", "READONLY"] = "MEMBER"


class UpdatePromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: Optional[str] = None
    qa_template: Optional[str] = Field(default=None, max_length=20_000)
    custom_persona: Optional[str] = Field(default=None, max_length=4_000)
    custom_entities: Optional[list[str]] = Field(default=None, max_length=50)


class TenantUserInfo(BaseModel):
    id: str
    tenant_id: str
    username: str
    role: str
    is_active: bool
    created_at: str


def _prompt_response(snapshot):
    return {
        "tenant_id": snapshot.tenant_id,
        "kb_id": snapshot.kb_id,
        "qa_template": snapshot.qa_template,
        "custom_persona": snapshot.custom_persona,
        "custom_entities": list(snapshot.custom_entities),
    }


class UpdateConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_k: Optional[int] = Field(default=None, ge=1, le=50)
    retrieval_mode: Optional[Literal["naive", "local", "global", "hybrid", "mix"]] = None
    system_persona: Optional[str] = Field(default=None, max_length=4000)
    max_file_size_bytes: Optional[int] = Field(default=None, ge=1024, le=1024 * 1024 * 1024)
    max_storage_bytes: Optional[int] = Field(default=None, ge=1024 * 1024, le=1024 * 1024 * 1024 * 1024)
    max_documents: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    max_active_tasks: Optional[int] = Field(default=None, ge=1, le=10_000)


@router.get("", response_model=list[TenantInfo])
async def list_tenants(
    current_user: User = Depends(require_role(Role.SYSTEM_ADMIN)),
    db: AsyncSession = Depends(get_db),
):
    """Lists all tenants; intentionally restricted to system administrators."""
    rows = (await db.execute(select(Tenant).order_by(Tenant.created_at.asc()))).scalars().all()
    return [
        TenantInfo(id=tenant.id, name=tenant.name, created_at=tenant.created_at.isoformat())
        for tenant in rows
    ]


@router.post("", response_model=TenantInfo, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    request: CreateTenantRequest,
    current_user: User = Depends(require_role(Role.SYSTEM_ADMIN)),
    db: AsyncSession = Depends(get_db),
):
    """Creates a tenant without exposing public self-registration."""
    tenant = Tenant(name=request.name)
    db.add(tenant)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise DomainValidationError("Tenant name is already in use or invalid") from exc
    await db.refresh(tenant)
    return TenantInfo(id=tenant.id, name=tenant.name, created_at=tenant.created_at.isoformat())


@router.get("/{tenant_id}/users", response_model=list[TenantUserInfo])
async def list_tenant_users(
    tenant_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lists users for the current tenant or any tenant for a system admin."""
    if current_user.role != Role.SYSTEM_ADMIN.value and current_user.tenant_id != tenant_id:
        raise AuthorizationError("Cannot inspect users from another tenant")
    rows = (
        await db.execute(
            select(User).where(User.tenant_id == tenant_id).order_by(User.created_at.asc())
        )
    ).scalars().all()
    return [
        TenantUserInfo(
            id=user.id,
            tenant_id=user.tenant_id,
            username=user.username,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at.isoformat(),
        )
        for user in rows
    ]


@router.post("/{tenant_id}/users", response_model=TenantUserInfo, status_code=status.HTTP_201_CREATED)
async def create_tenant_user(
    tenant_id: str,
    request: CreateTenantUserRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Creates a member for the current tenant or any tenant by a system admin."""
    if current_user.role != Role.SYSTEM_ADMIN.value and (
        current_user.role != Role.TENANT_ADMIN.value or current_user.tenant_id != tenant_id
    ):
        raise AuthorizationError("Only a tenant administrator can manage these users")
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if not tenant:
        raise DomainValidationError("Target tenant does not exist")
    user = User(
        tenant_id=tenant_id,
        username=request.username,
        hashed_password=get_password_hash(request.password),
        role=request.role,
        is_active=True,
    )
    db.add(user)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise DomainValidationError("Username is already in use or invalid") from exc
    await db.refresh(user)
    return TenantUserInfo(
        id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at.isoformat(),
    )


@router.get("/prompts")
async def get_tenant_prompts(
    kb_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if kb_id:
        kb = (
            await db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.id == kb_id,
                    KnowledgeBase.tenant_id == current_user.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not kb:
            raise DomainValidationError("Knowledge base does not belong to the current tenant")
    snapshot = await TenantPromptManager.load_snapshot_db(
        db, current_user.tenant_id, kb_id
    )
    return _prompt_response(snapshot)


@router.put("/prompts")
async def update_tenant_prompts(
    request: UpdatePromptRequest,
    current_user: User = Depends(require_role(Role.TENANT_ADMIN)),
    db: AsyncSession = Depends(get_db),
):
    if request.kb_id:
        kb = (
            await db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.id == request.kb_id,
                    KnowledgeBase.tenant_id == current_user.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not kb:
            raise DomainValidationError("Knowledge base does not belong to the current tenant")
    await TenantPromptManager.set_tenant_overlay_db(
        db,
        tenant_id=current_user.tenant_id,
        kb_id=request.kb_id,
        qa_template=request.qa_template,
        custom_persona=request.custom_persona,
        custom_entities=request.custom_entities,
    )
    snapshot = await TenantPromptManager.load_snapshot_db(
        db, current_user.tenant_id, request.kb_id
    )
    return _prompt_response(snapshot)


@router.get("/config", response_model=TenantRuntimeConfig)
async def get_tenant_config(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieves active runtime configuration for the authenticated tenant."""
    return await TenantConfigManager.load_latest_config_db(
        db,
        tenant_id=current_user.tenant_id,
    )


@router.put("/config", response_model=TenantRuntimeConfig)
async def update_tenant_config(
    request: UpdateConfigRequest,
    current_user: User = Depends(require_role(Role.TENANT_ADMIN)),
    db: AsyncSession = Depends(get_db),
):
    """Atomically updates tenant configuration with versioning and validation rollback."""
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    return await TenantConfigManager.publish_config_db(
        db,
        tenant_id=current_user.tenant_id,
        updates=updates
    )
