import sys
sys.path.insert(0, "e:/resume/rag-api-pro")

import asyncio
from sqlalchemy import select
from src.core.database import AsyncSessionLocal
from src.models import db_models
from src.core.security import get_password_hash
from src.core.security.rbac import Role

async def create_users():
    async with AsyncSessionLocal() as session:
        # Find default tenant
        stmt = select(db_models.Tenant).where(db_models.Tenant.id == "default_tenant")
        res = await session.execute(stmt)
        tenant = res.scalar_one_or_none()
        if not tenant:
            tenant = db_models.Tenant(id="default_tenant", name="default")
            session.add(tenant)
            await session.flush()

        accounts = [
            {
                "id": "default_sysadmin",
                "username": "sysadmin",
                "role": Role.SYSTEM_ADMIN.value,
                "desc": "系统超级管理员 (SYSTEM_ADMIN, Level 40)"
            },
            {
                "id": "default_member",
                "username": "member",
                "role": Role.MEMBER.value,
                "desc": "普通业务专员 (MEMBER, Level 20)"
            },
            {
                "id": "default_viewer",
                "username": "viewer",
                "role": Role.READONLY.value,
                "desc": "只读审计访客 (READONLY, Level 10)"
            },
            # Also create alias usernames for convenience
            {
                "id": "default_readonly_user",
                "username": "readonly_user",
                "role": Role.READONLY.value,
                "desc": "只读用户 (READONLY, Level 10)"
            },
            {
                "id": "default_member_user",
                "username": "member_user",
                "role": Role.MEMBER.value,
                "desc": "成员用户 (MEMBER, Level 20)"
            }
        ]

        pwd_hash = get_password_hash("password123")

        created = []
        for acc in accounts:
            # Check if user already exists
            u_stmt = select(db_models.User).where(
                db_models.User.tenant_id == tenant.id,
                db_models.User.username == acc["username"]
            )
            u_res = await session.execute(u_stmt)
            existing_user = u_res.scalar_one_or_none()
            if existing_user:
                existing_user.role = acc["role"]
                existing_user.hashed_password = pwd_hash
                created.append(f"Updated: {acc['username']} -> {acc['role']}")
            else:
                new_user = db_models.User(
                    id=acc["id"],
                    tenant_id=tenant.id,
                    username=acc["username"],
                    hashed_password=pwd_hash,
                    role=acc["role"]
                )
                session.add(new_user)
                created.append(f"Created: {acc['username']} -> {acc['role']}")

        await session.commit()
        print("RBAC Users Synchronization Successful:")
        for c in created:
            print("  *", c)

if __name__ == "__main__":
    asyncio.run(create_users())
