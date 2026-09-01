"""
Database Initialization & Seed Data Script
Creates all tables and seeds:
- Default Tenant: 'tenant_default' (ID: 'tenant_default_01')
- System Admin: 'admin' / 'admin123'
- Regular User: 'test_user' / 'test123'
- Secondary Tenant for Negative Testing: 'tenant_beta' (ID: 'tenant_beta_01')
- Secondary User: 'beta_user' / 'beta123'
- Default Knowledge Base: 'kb_default' (ID: 'kb_default_01')
- Beta Knowledge Base: 'kb_beta' (ID: 'kb_beta_01')
"""
import os
import sys
import asyncio

# Ensure utf-8 stdout on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.config import settings
from src.core.database import Base
from src.core.security import get_password_hash
from src.models.db_models import Tenant, User, KnowledgeBase, Document, Task
from src.core.clients import InfrastructureClients
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select


async def init_db(drop_first: bool = False):
    print("[INIT] Connecting to PostgreSQL and creating tables...")
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    
    async with engine.begin() as conn:
        if drop_first or "--drop" in sys.argv:
            print("[WARN] Dropping all existing tables for schema update...")
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("[SUCCESS] Tables created successfully.")

    print("[INIT] Initializing Infrastructure components (MinIO, Redis, Qdrant, Memgraph)...")
    await InfrastructureClients.init_infrastructure()
    print("[SUCCESS] Infrastructure components initialized.")

    SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with SessionLocal() as db:
        # 1. Seed Default Tenant
        res = await db.execute(select(Tenant).where(Tenant.name == "tenant_default"))
        t_default = res.scalar_one_or_none()
        if not t_default:
            t_default = Tenant(id="tenant_default_01", name="tenant_default")
            db.add(t_default)
            print("[SEED] Created Tenant: tenant_default (tenant_default_01)")

        # 2. Seed Beta Tenant (For cross-tenant isolation test)
        res = await db.execute(select(Tenant).where(Tenant.name == "tenant_beta"))
        t_beta = res.scalar_one_or_none()
        if not t_beta:
            t_beta = Tenant(id="tenant_beta_01", name="tenant_beta")
            db.add(t_beta)
            print("[SEED] Created Tenant: tenant_beta (tenant_beta_01)")

        await db.flush()

        # 3. Seed Users
        res = await db.execute(select(User).where(User.username == "admin", User.tenant_id == t_default.id))
        admin = res.scalar_one_or_none()
        if not admin:
            admin = User(
                id="user_admin_01",
                tenant_id=t_default.id,
                username="admin",
                hashed_password=get_password_hash("admin123"),
                role="SYSTEM_ADMIN",
            )
            db.add(admin)
            print("[SEED] Created User: admin (admin123, SYSTEM_ADMIN)")

        res = await db.execute(select(User).where(User.username == "test_user", User.tenant_id == t_default.id))
        test_user = res.scalar_one_or_none()
        if not test_user:
            test_user = User(
                id="user_test_01",
                tenant_id=t_default.id,
                username="test_user",
                hashed_password=get_password_hash("test123"),
                role="MEMBER",
            )
            db.add(test_user)
            print("[SEED] Created User: test_user (test123, MEMBER)")

        res = await db.execute(select(User).where(User.username == "beta_user", User.tenant_id == t_beta.id))
        beta_user = res.scalar_one_or_none()
        if not beta_user:
            beta_user = User(
                id="user_beta_01",
                tenant_id=t_beta.id,
                username="beta_user",
                hashed_password=get_password_hash("beta123"),
                role="MEMBER",
            )
            db.add(beta_user)
            print("[SEED] Created User: beta_user (beta123, MEMBER in tenant_beta)")

        # 4. Seed Knowledge Bases
        res = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == "kb_default_01"))
        kb_default = res.scalar_one_or_none()
        if not kb_default:
            kb_default = KnowledgeBase(
                id="kb_default_01",
                tenant_id=t_default.id,
                name="Default Tech Knowledge Base",
                description="Default Knowledge Base for tenant_default",
            )
            db.add(kb_default)
            print("[SEED] Created Knowledge Base: kb_default_01 (Default Tech Knowledge Base)")

        res = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == "kb_beta_01"))
        kb_beta = res.scalar_one_or_none()
        if not kb_beta:
            kb_beta = KnowledgeBase(
                id="kb_beta_01",
                tenant_id=t_beta.id,
                name="Beta Test Knowledge Base",
                description="Isolated Knowledge Base for tenant_beta",
            )
            db.add(kb_beta)
            print("[SEED] Created Knowledge Base: kb_beta_01 (Beta Test Knowledge Base)")

        await db.commit()

    await InfrastructureClients.close_all()
    await engine.dispose()
    print("[SUCCESS] Database and seed data initialization completed successfully!")


if __name__ == "__main__":
    asyncio.run(init_db())
