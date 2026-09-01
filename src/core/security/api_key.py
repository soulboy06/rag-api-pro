"""
Level 7: API Key Generator & SHA-256 Hashed Credential Manager
Generates cryptographically secure API keys, stores only SHA-256 hashes, and handles validation and revocation.
Fixes: P0-SEC-01, P2-OBS-08
"""
import secrets
import hashlib
from datetime import datetime, timezone
from typing import Tuple, Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.db_models import ApiKey, utcnow
from src.core.exceptions import AuthenticationError


class ApiKeyManager:
    PREFIX = "rk_live_"

    @classmethod
    def generate_api_key(cls) -> Tuple[str, str, str]:
        """
        Generates a new API key.
        Returns: (raw_key, key_prefix, key_hash)
        The raw_key is only shown ONCE to the user upon creation.
        """
        random_part = secrets.token_hex(24)
        raw_key = f"{cls.PREFIX}{random_part}"
        prefix = raw_key[:12]
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return raw_key, prefix, key_hash

    @classmethod
    def hash_key(cls, raw_key: str) -> str:
        """Hashes raw key with SHA-256."""
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @classmethod
    async def authenticate_key(cls, session: AsyncSession, raw_key: str) -> ApiKey:
        """
        Validates raw API key by checking SHA-256 hash in database.
        Updates last_used_at timestamp on successful validation.
        """
        if not raw_key or not raw_key.startswith(cls.PREFIX):
            raise AuthenticationError("Invalid API key format")

        key_hash = cls.hash_key(raw_key)
        stmt = (
            select(ApiKey)
            .where(
                ApiKey.key_hash == key_hash,
                ApiKey.is_active == True
            )
        )
        res = await session.execute(stmt)
        api_key_record = res.scalar_one_or_none()

        if not api_key_record:
            raise AuthenticationError("Invalid or revoked API key")

        # Check expiration
        now = utcnow()
        if api_key_record.expires_at and api_key_record.expires_at < now:
            raise AuthenticationError("API key has expired")

        # Update last_used_at
        api_key_record.last_used_at = now
        await session.commit()
        return api_key_record
