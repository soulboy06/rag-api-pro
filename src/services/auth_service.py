"""
Authentication Service
Handles user authentication, password verification, and JWT generation.
"""
from typing import Optional
import hashlib
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.models.db_models import User, Tenant
from src.models.schemas import UserLogin, TokenResponse
from src.core.security import verify_password
from src.core.security.jwt import JWTManager
from src.core.config import settings
from src.core.exceptions import AuthenticationError, ResourceNotFoundError


class AuthService:
    @staticmethod
    async def authenticate_user(
        db: AsyncSession,
        login_data: UserLogin
    ) -> TokenResponse:
        # 1. Resolve Tenant
        tenant_stmt = select(Tenant).where(Tenant.name == login_data.tenant_name)
        tenant_res = await db.execute(tenant_stmt)
        tenant = tenant_res.scalar_one_or_none()
        if not tenant:
            raise AuthenticationError(f"Tenant '{login_data.tenant_name}' not found")

        # 2. Resolve User
        user_stmt = select(User).where(
            User.tenant_id == tenant.id,
            User.username == login_data.username
        )
        user_res = await db.execute(user_stmt)
        user = user_res.scalar_one_or_none()
        if not user or not user.is_active or not verify_password(login_data.password, user.hashed_password):
            raise AuthenticationError("Invalid username or password")

        # 3. Create JWT
        access_token = JWTManager.create_access_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
            username=user.username,
            role=user.role,
        )
        refresh_token = JWTManager.create_refresh_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
        )
        user.refresh_token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        await db.commit()

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            token_type="bearer",
            user_id=user.id,
            tenant_id=user.tenant_id,
            username=user.username,
            role=user.role,
        )
