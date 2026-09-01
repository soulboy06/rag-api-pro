"""
Level 4: Tenant Configuration Versioning & Safe Hot-Reloading
Enforces atomic configuration publication, version validation, PostgreSQL persistence, and snapshot pinning for in-flight tasks.
Fixes: P1-CORE-08, P1-CORE-09, P1-CORE-10, P1-CORE-11, P1-CORE-12, P1-API-10
"""
import copy
from typing import Dict, Any, Optional, Literal
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, text

from src.core.exceptions import ValidationError as CustomValidationError


class TenantRuntimeConfig(BaseModel):
    tenant_id: str
    version_id: int = 1
    llm_model: str = "glm-4-flash"
    embedding_model: str = "embedding-3"
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    top_k: int = Field(default=8, ge=1, le=50)
    score_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    enable_graph_rerank: bool = True
    enable_hybrid_search: bool = True
    max_tokens: int = Field(default=2048, ge=128, le=16384)
    retrieval_mode: Literal["naive", "local", "global", "hybrid", "mix"] = "hybrid"
    system_persona: Optional[str] = Field(default=None, max_length=4000)
    max_file_size_bytes: int = Field(default=50 * 1024 * 1024, ge=1 * 1024, le=1024 * 1024 * 1024)
    max_storage_bytes: int = Field(default=5 * 1024 * 1024 * 1024, ge=1 * 1024 * 1024, le=1024 * 1024 * 1024 * 1024)
    max_documents: int = Field(default=10_000, ge=1, le=10_000_000)
    max_active_tasks: int = Field(default=10, ge=1, le=10_000)


class TenantConfigManager:
    """
    Manages versioned configuration snapshots per tenant.
    Guarantees atomic publishing, in-memory caching, and PostgreSQL persistence.
    """
    # Active latest versions per tenant: tenant_id -> TenantRuntimeConfig
    _active_configs: Dict[str, TenantRuntimeConfig] = {}
    # Historical version archive: (tenant_id, version_id) -> TenantRuntimeConfig
    _version_history: Dict[tuple, TenantRuntimeConfig] = {}
    MAX_CACHED_TENANTS = 1000
    MAX_VERSIONS_PER_TENANT = 20

    @classmethod
    def _cache_config(cls, config: TenantRuntimeConfig) -> None:
        """Stores bounded hot snapshots; PostgreSQL remains the source of truth."""
        tenant_id = config.tenant_id
        if tenant_id not in cls._active_configs and len(cls._active_configs) >= cls.MAX_CACHED_TENANTS:
            evicted_tenant, _ = next(iter(cls._active_configs.items()))
            cls._active_configs.pop(evicted_tenant, None)
            for key in [key for key in cls._version_history if key[0] == evicted_tenant]:
                cls._version_history.pop(key, None)
        cls._active_configs[tenant_id] = config
        cls._version_history[(tenant_id, config.version_id)] = config
        versions = sorted(
            version for cached_tenant, version in cls._version_history
            if cached_tenant == tenant_id
        )
        for version in versions[:-cls.MAX_VERSIONS_PER_TENANT]:
            cls._version_history.pop((tenant_id, version), None)

    @classmethod
    def get_latest_config(cls, tenant_id: str) -> TenantRuntimeConfig:
        """Retrieves the latest published configuration for a tenant (or default v1)."""
        if tenant_id not in cls._active_configs:
            default_config = TenantRuntimeConfig(tenant_id=tenant_id, version_id=1)
            cls._cache_config(default_config)
        return cls._active_configs[tenant_id]

    @classmethod
    async def load_latest_config_db(cls, db: AsyncSession, tenant_id: str) -> TenantRuntimeConfig:
        """Loads latest configuration from PostgreSQL into memory cache."""
        from src.models.db_models import TenantConfig
        stmt = (
            select(TenantConfig)
            .where(TenantConfig.tenant_id == tenant_id)
            .order_by(desc(TenantConfig.version_id))
            .limit(1)
        )
        res = await db.execute(stmt)
        db_cfg = res.scalar_one_or_none()
        if db_cfg and db_cfg.config_data:
            try:
                cfg = TenantRuntimeConfig(**db_cfg.config_data)
            except ValidationError as ve:
                raise CustomValidationError(
                    message=f"Persisted tenant configuration is invalid: {ve}",
                    details={"errors": ve.errors()},
                ) from ve
            cls._cache_config(cfg)
            return cfg

        # PostgreSQL is authoritative. A process restart, a new database, or
        # a deliberate deletion of the persisted row must not inherit a stale
        # in-memory snapshot from an earlier lifecycle.
        default_config = TenantRuntimeConfig(tenant_id=tenant_id, version_id=1)
        cls._cache_config(default_config)
        return default_config

    @classmethod
    def get_versioned_snapshot(cls, tenant_id: str, version_id: Optional[int] = None) -> TenantRuntimeConfig:
        """
        Retrieves a pinned configuration snapshot.
        If version_id is provided, returns that specific version (ensuring in-flight task stability).
        """
        if version_id is not None:
            key = (tenant_id, version_id)
            if key in cls._version_history:
                return cls._version_history[key]

        # Default to latest active
        return cls.get_latest_config(tenant_id)

    @classmethod
    def publish_config(cls, tenant_id: str, updates: Dict[str, Any]) -> TenantRuntimeConfig:
        """
        Atomically validates and publishes a new configuration version in memory.
        Rolls back and retains existing version if validation fails.
        """
        current_config = cls.get_latest_config(tenant_id)
        new_version_id = current_config.version_id + 1

        # Build candidate config dictionary
        config_data = current_config.model_dump()
        config_data.update(updates)
        config_data["tenant_id"] = tenant_id
        config_data["version_id"] = new_version_id

        # Validate with Pydantic
        try:
            new_config = TenantRuntimeConfig(**config_data)
        except ValidationError as ve:
            raise CustomValidationError(
                message=f"Invalid tenant configuration update: {str(ve)}",
                details={"errors": ve.errors()}
            )

        # Atomic commit to memory
        cls._cache_config(new_config)
        return new_config

    @classmethod
    async def publish_config_db(
        cls,
        db: AsyncSession,
        tenant_id: str,
        updates: Dict[str, Any]
    ) -> TenantRuntimeConfig:
        """Atomically publishes new configuration version to memory and PostgreSQL."""
        from src.models.db_models import TenantConfig, utcnow

        # Serialize the no-row-yet and version-allocation cases across API
        # replicas. A row lock alone cannot protect two concurrent first
        # publications for the same tenant.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"rag-api-pro:tenant-config:{tenant_id}"},
        )

        # Read the database version first.  The process-local cache may be
        # empty after a restart or stale when multiple API replicas publish.
        latest_stmt = (
            select(TenantConfig)
            .where(TenantConfig.tenant_id == tenant_id)
            .order_by(desc(TenantConfig.version_id))
            .limit(1)
            .with_for_update()
        )
        latest = (await db.execute(latest_stmt)).scalar_one_or_none()
        if latest and latest.config_data:
            current_data = dict(latest.config_data)
            current_version = latest.version_id
        else:
            current_data = TenantRuntimeConfig(
                tenant_id=tenant_id,
                version_id=1,
            ).model_dump()
            current_version = 1

        current_data.update(updates)
        current_data["tenant_id"] = tenant_id
        current_data["version_id"] = current_version + 1 if latest else current_version
        try:
            config = TenantRuntimeConfig(**current_data)
        except ValidationError as ve:
            raise CustomValidationError(
                message=f"Invalid tenant configuration update: {ve}",
                details={"errors": ve.errors()},
            ) from ve

        db_cfg = TenantConfig(
            tenant_id=tenant_id,
            version_id=config.version_id,
            config_data=config.model_dump(),
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(db_cfg)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        # Cache is updated only after the durable commit succeeds.
        cls._cache_config(config)
        return config

    @classmethod
    def reset(cls) -> None:
        """Resets all configurations (used for test tear down)."""
        cls._active_configs.clear()
        cls._version_history.clear()
