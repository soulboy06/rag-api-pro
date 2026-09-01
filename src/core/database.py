"""
Database Connection & Session Management
Uses SQLAlchemy 2.0 async engine with asyncpg and NullPool for clean connection lifecycle.
"""
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
    AsyncEngine
)
from sqlalchemy.orm import declarative_base
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from src.core.config import settings

Base = declarative_base()

engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    # The Windows development/test runner recreates event loops between
    # pytest fixtures.  Reusing asyncpg connections across those loops causes
    # closed-loop failures, so development keeps a clean lifecycle.  The
    # production profile uses SQLAlchemy's bounded QueuePool below.
    **(
        {"poolclass": NullPool}
        if settings.APP_ENV in {"development", "test"}
        else {
            "pool_size": settings.POOL_SIZE,
            "max_overflow": settings.MAX_OVERFLOW,
            "pool_recycle": settings.POOL_RECYCLE_SECONDS,
        }
    ),
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def init_db():
    """Ensures all database tables defined in models are created and default data is seeded."""
    import src.models.db_models as db_models
    from sqlalchemy import select
    from src.core.security import get_password_hash

    async with engine.begin() as conn:
        # API and Worker can start together.  Serialize bootstrap DDL so two
        # processes cannot race while creating the same tables/indexes.
        await conn.execute(text("SELECT pg_advisory_lock(hashtext('rag-api-pro:db-bootstrap'))"))
        try:
            await conn.run_sync(Base.metadata.create_all)
            # Keep existing development volumes compatible until migrations are
            # introduced; the advisory lock makes this bridge multi-process safe.
            await conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ"
            ))
            await conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_options JSONB"
            ))
            await conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS target_generation INTEGER NOT NULL DEFAULT 1"
            ))
            await conn.execute(text(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS active_generation INTEGER NOT NULL DEFAULT 1"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS refresh_token_hash VARCHAR(64)"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_tenant_idempotency_nonnull "
                "ON tasks (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
            ))
            # PostgreSQL treats NULL values as distinct in a normal unique
            # constraint; this partial index enforces one tenant-wide prompt
            # overlay while keeping one overlay per (tenant, knowledge base).
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_prompts_tenant_scope_null "
                "ON tenant_prompts (tenant_id) WHERE kb_id IS NULL"
            ))
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(hashtext('rag-api-pro:db-bootstrap'))"))

    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('rag-api-pro:seed-data'))"))
        # Check if default tenant exists
        stmt = select(db_models.Tenant).where(db_models.Tenant.name == "default")
        res = await session.execute(stmt)
        default_tenant = res.scalar_one_or_none()
        if not default_tenant and settings.SEED_DEMO_DATA:
            default_tenant = db_models.Tenant(
                id="default_tenant",
                name="default",
            )
            session.add(default_tenant)
            await session.flush()

            # Create default users for all 4 RBAC roles
            roles_to_seed = [
                ("default_admin", "admin", "TENANT_ADMIN"),
                ("default_sysadmin", "sysadmin", "SYSTEM_ADMIN"),
                ("default_member", "member", "MEMBER"),
                ("default_viewer", "viewer", "READONLY"),
            ]
            for uid, uname, urole in roles_to_seed:
                session.add(
                    db_models.User(
                        id=uid,
                        tenant_id=default_tenant.id,
                        username=uname,
                        hashed_password=get_password_hash("password123"),
                        role=urole,
                    )
                )

            # Create default knowledge base
            default_kb = db_models.KnowledgeBase(
                id="default_kb",
                tenant_id=default_tenant.id,
                name="默认综合知识库",
                description="系统默认知识库"
            )
            session.add(default_kb)
            await session.commit()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for providing an async database session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
