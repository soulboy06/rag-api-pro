"""
Level 7: Authentication & API Key Management Router
Provides login, token refresh, and full API Key lifecycle management.
Fixes: P0-SEC-01, P1-API-05, P2-OBS-08
"""
from typing import List, Optional
import hashlib
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete

from src.core.database import get_db
from src.core.config import settings
from src.models.schemas import UserLogin, TokenResponse, UserInfo
from src.services.auth_service import AuthService
from src.api.deps import get_current_user, require_role
from src.core.security.jwt import JWTManager
from src.core.security.api_key import ApiKeyManager
from src.core.security.rbac import Role, ROLE_HIERARCHY
from src.models.db_models import User, ApiKey
from src.core.exceptions import ResourceNotFoundError, AuthenticationError

router = APIRouter(prefix="/auth", tags=["Authentication"])


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    role: Optional[str] = Field(default="MEMBER")
    expires_in_days: Optional[int] = Field(default=30, ge=1, le=365)


class ApiKeyCreatedResponse(BaseModel):
    id: str
    name: str
    raw_api_key: str  # Only displayed once!
    key_prefix: str
    role: str
    expires_at: Optional[str]


class ApiKeyInfo(BaseModel):
    id: str
    name: str
    key_prefix: str
    role: str
    is_active: bool
    expires_at: Optional[str]
    created_at: str
    last_used_at: Optional[str]


@router.post("/login", response_model=TokenResponse)
async def login(
    login_data: UserLogin,
    db: AsyncSession = Depends(get_db)
):
    """Authenticates user and returns signed Access & Refresh JWT."""
    return await AuthService.authenticate_user(db, login_data)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db)
):
    """Refreshes an expired Access Token using a valid Refresh Token."""
    payload = JWTManager.decode_token(request.refresh_token)
    if payload.get("type") != "refresh":
        raise AuthenticationError("Invalid token type for refresh")

    user_id = payload.get("sub")
    tenant_id = payload.get("tenant_id")
    stmt = select(User).where(User.id == user_id, User.tenant_id == tenant_id)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    if not user or not user.is_active:
        raise AuthenticationError("User not found or disabled")
    token_hash = hashlib.sha256(request.refresh_token.encode("utf-8")).hexdigest()
    if not user.refresh_token_hash or user.refresh_token_hash != token_hash:
        raise AuthenticationError("Refresh token has been rotated or revoked")

    access_token = JWTManager.create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        role=user.role
    )
    new_refresh_token = JWTManager.create_refresh_token(
        user_id=user.id,
        tenant_id=user.tenant_id
    )
    user.refresh_token_hash = hashlib.sha256(new_refresh_token.encode("utf-8")).hexdigest()
    await db.commit()
    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        token_type="bearer",
        user_id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        role=user.role,
    )


@router.get("/me", response_model=UserInfo)
async def get_me(
    current_user: User = Depends(get_current_user)
):
    """Returns profile of currently authenticated user."""
    return UserInfo(
        id=current_user.id,
        tenant_id=current_user.tenant_id,
        username=current_user.username,
        role=current_user.role,
        created_at=current_user.created_at,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revokes the current user's refresh-token family."""
    user = await db.get(User, current_user.id)
    if user:
        user.refresh_token_hash = None
        await db.commit()


# ---------------- API Key Management ----------------

@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    request: CreateApiKeyRequest,
    current_user: User = Depends(require_role(Role.TENANT_ADMIN)),
    db: AsyncSession = Depends(get_db)
):
    """Generates a new API Key for the active tenant. Returns raw key once."""
    target_role = (request.role or Role.MEMBER.value).upper()
    valid_roles = {r.value for r in Role}
    if target_role not in valid_roles:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid role: '{target_role}'. Valid options are: {list(valid_roles)}"
        )

    caller_level = ROLE_HIERARCHY.get(current_user.role, 0)
    target_level = ROLE_HIERARCHY.get(target_role, 0)
    if target_level > caller_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Privilege escalation denied: cannot create API Key with role '{target_role}' higher than caller role '{current_user.role}'"
        )

    raw_key, prefix, key_hash = ApiKeyManager.generate_api_key()
    expires_at = datetime.now(timezone.utc) + timedelta(days=request.expires_in_days) if request.expires_in_days else None

    api_key = ApiKey(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        name=request.name,
        key_prefix=prefix,
        key_hash=key_hash,
        role=target_role,
        expires_at=expires_at,
        is_active=True
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return ApiKeyCreatedResponse(
        id=api_key.id,
        name=api_key.name,
        raw_api_key=raw_key,
        key_prefix=prefix,
        role=api_key.role,
        expires_at=expires_at.isoformat() if expires_at else None
    )


@router.get("/api-keys", response_model=List[ApiKeyInfo])
async def list_api_keys(
    current_user: User = Depends(require_role(Role.TENANT_ADMIN)),
    db: AsyncSession = Depends(get_db)
):
    """Lists all API keys for the current tenant."""
    stmt = select(ApiKey).where(ApiKey.tenant_id == current_user.tenant_id)
    res = await db.execute(stmt)
    keys = res.scalars().all()

    return [
        ApiKeyInfo(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            role=k.role,
            is_active=k.is_active,
            expires_at=k.expires_at.isoformat() if k.expires_at else None,
            created_at=k.created_at.isoformat(),
            last_used_at=k.last_used_at.isoformat() if k.last_used_at else None
        )
        for k in keys
    ]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: str,
    current_user: User = Depends(require_role(Role.TENANT_ADMIN)),
    db: AsyncSession = Depends(get_db)
):
    """Revokes/deactivates an API key."""
    stmt = select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == current_user.tenant_id)
    res = await db.execute(stmt)
    key_record = res.scalar_one_or_none()
    if not key_record:
        raise ResourceNotFoundError(f"API key '{key_id}' not found")

    key_record.is_active = False
    await db.commit()
