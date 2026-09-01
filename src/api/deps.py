"""
Level 7: API Gateway Dependencies with Dual-Mode (JWT & API Key) Authentication
Resolves authenticated user, tenant context, and enforces RBAC permission checks.
Fixes: P0-SEC-01, P1-API-05, P1-API-06, P2-OBS-08
"""
from typing import Dict, Any, Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.core.database import get_db
from src.core.security.jwt import JWTManager
from src.core.security.api_key import ApiKeyManager
from src.core.security.rbac import Role, ROLE_HIERARCHY
from src.core.tenant.context import set_current_tenant_context, TenantContext
from src.models.db_models import User, ApiKey
from src.core.logger import tenant_id_ctx, user_id_ctx
from src.core.exceptions import AuthenticationError, AuthorizationError

security_bearer = HTTPBearer(auto_error=True)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security_bearer),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Dual-mode authentication:
    1. If token starts with 'rk_live_', authenticates as API Key.
    2. Otherwise, decodes as JWT Access Token.
    Binds active TenantContext.
    """
    raw_token = credentials.credentials.strip()

    if raw_token.startswith(ApiKeyManager.PREFIX):
        # Mode B: API Key Authentication
        api_key_record = await ApiKeyManager.authenticate_key(db, raw_token)
        stmt = select(User).where(User.id == api_key_record.user_id)
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()
        if not user or not user.is_active:
            raise AuthenticationError("User associated with API key not found")
        # Override user role in-memory for this request and expunge to prevent DB persistence
        user.role = api_key_record.role
        db.expunge(user)
    else:
        # Mode A: JWT Authentication
        payload = JWTManager.decode_token(raw_token)
        if payload.get("type") != "access":
            raise AuthenticationError("Refresh tokens cannot be used to access API endpoints")

        user_id = payload.get("sub")
        if not user_id:
            raise AuthenticationError("Token missing 'sub' claim")

        stmt = select(User).where(User.id == user_id)
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()
        if not user or not user.is_active:
            raise AuthenticationError("User not found or disabled")
        if payload.get("tenant_id") != user.tenant_id:
            raise AuthenticationError("Token tenant does not match the authenticated user")

    # Set tenant context
    request.state.tenant_id = user.tenant_id
    request.state.user_id = user.id
    tenant_id_ctx.set(user.tenant_id)
    user_id_ctx.set(user.id)
    set_current_tenant_context(
        TenantContext(
            tenant_id=user.tenant_id,
            user_id=user.id,
            kb_id=None
        )
    )
    return user


def require_role(min_role: Role):
    """Dependency to enforce minimum RBAC role."""
    min_level = ROLE_HIERARCHY.get(min_role.value, 20)

    async def _checker(current_user: User = Depends(get_current_user)) -> User:
        user_role = getattr(current_user, "role", Role.READONLY.value)
        user_level = ROLE_HIERARCHY.get(user_role, 0)
        if user_level < min_level:
            raise AuthorizationError(
                f"Insufficient permissions: requires '{min_role.value}', current user has '{user_role}'"
            )
        return current_user

    return _checker
