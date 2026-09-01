"""
Level 7: JWT Authentication & Token Lifecycle Management
Signs and verifies Access & Refresh JWT tokens using python-jose with strict expiration and tenant context extraction.
Fixes: P0-SEC-01, P1-API-05
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
import uuid
from jose import jwt, JWTError

from src.core.config import settings
from src.core.exceptions import AuthenticationError

ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 7


class JWTManager:
    @classmethod
    def create_access_token(
        cls,
        user_id: str,
        tenant_id: str,
        username: str,
        role: str = "MEMBER",
        expires_delta: Optional[timedelta] = None
    ) -> str:
        now = datetime.now(timezone.utc)
        expire = now + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
        payload = {
            "sub": user_id,
            "tenant_id": tenant_id,
            "username": username,
            "role": role,
            "type": "access",
            "iat": int(now.timestamp()),
            "exp": int(expire.timestamp())
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    @classmethod
    def create_refresh_token(
        cls,
        user_id: str,
        tenant_id: str
    ) -> str:
        now = datetime.now(timezone.utc)
        expire = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        payload = {
            "sub": user_id,
            "tenant_id": tenant_id,
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "iat": int(now.timestamp()),
            "exp": int(expire.timestamp())
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    @classmethod
    def decode_token(cls, token: str) -> Dict[str, Any]:
        """Decodes and validates a JWT token signature and expiration."""
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM]
            )
            return payload
        except JWTError:
            raise AuthenticationError("Invalid or expired authentication token")
