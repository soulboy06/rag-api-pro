import sys
sys.path.insert(0, "e:/resume/rag-api-pro")

import asyncio
from sqlalchemy import select
from src.core.database import AsyncSessionLocal
from src.models import db_models
from src.core.security import get_password_hash
from src.core.security.rbac import Role

async def setup_alpha_tenant():
    async with AsyncSessionLocal() as session:
        # 1. Create tenant_alpha
        stmt = select(db_models.Tenant).where(db_models.Tenant.id == "tenant_alpha")
        res = await session.execute(stmt)
        tenant = res.scalar_one_or_none()
        if not tenant:
            tenant = db_models.Tenant(
                id="tenant_alpha",
                name="alpha"
            )
            session.add(tenant)
            await session.flush()
            print("Created Tenant: tenant_alpha (name: alpha)")
        else:
            tenant.name = "alpha"
            print("Found existing Tenant: tenant_alpha (name: alpha)")

        pwd_hash = get_password_hash("password123")

        # 2. Create alpha_admin user
        u_stmt = select(db_models.User).where(
            db_models.User.tenant_id == "tenant_alpha",
            db_models.User.username == "alpha_admin"
        )
        u_res = await session.execute(u_stmt)
        user = u_res.scalar_one_or_none()
        if not user:
            user = db_models.User(
                id="user_alpha_admin",
                tenant_id="tenant_alpha",
                username="alpha_admin",
                hashed_password=pwd_hash,
                role=Role.TENANT_ADMIN.value
            )
            session.add(user)
            print("Created User: alpha_admin (Role: TENANT_ADMIN, Tenant: alpha)")
        else:
            user.hashed_password = pwd_hash
            user.role = Role.TENANT_ADMIN.value
            print("Updated User: alpha_admin (Role: TENANT_ADMIN, Tenant: alpha)")

        # 3. Create alpha_member and alpha_viewer for completeness
        for uname, urole in [("alpha_member", Role.MEMBER.value), ("alpha_viewer", Role.READONLY.value)]:
            sub_u_stmt = select(db_models.User).where(
                db_models.User.tenant_id == "tenant_alpha",
                db_models.User.username == uname
            )
            sub_u_res = await session.execute(sub_u_stmt)
            sub_user = sub_u_res.scalar_one_or_none()
            if not sub_user:
                session.add(
                    db_models.User(
                        id=f"user_{uname}",
                        tenant_id="tenant_alpha",
                        username=uname,
                        hashed_password=pwd_hash,
                        role=urole
                    )
                )
                print(f"Created User: {uname} (Role: {urole}, Tenant: alpha)")

        # 4. Create Knowledge Base for Alpha
        kb_stmt = select(db_models.KnowledgeBase).where(db_models.KnowledgeBase.id == "kb_alpha_01")
        kb_res = await session.execute(kb_stmt)
        kb = kb_res.scalar_one_or_none()
        if not kb:
            kb = db_models.KnowledgeBase(
                id="kb_alpha_01",
                tenant_id="tenant_alpha",
                name="Alpha 独立企业知识库",
                description="Alpha 专属空间，与 default 租户数据 100% 物理隔离"
            )
            session.add(kb)
            print("Created Knowledge Base: kb_alpha_01 for tenant_alpha")
        else:
            kb.name = "Alpha 独立企业知识库"
            print("Found Knowledge Base: kb_alpha_01")

        await session.commit()
        print("\nSetup complete for Tenant Alpha!")

if __name__ == "__main__":
    asyncio.run(setup_alpha_tenant())
