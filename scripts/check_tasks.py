import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import asyncio
from sqlalchemy import select, desc
from src.core.database import AsyncSessionLocal
from src.models.db_models import Task, Document

async def check():
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(Task.status == "RUNNING")
        res = await session.execute(stmt)
        tasks = res.scalars().all()
        print(f"Running tasks: {len(tasks)}")
        for t in tasks:
            doc_stmt = select(Document).where(Document.id == t.doc_id)
            doc_res = await session.execute(doc_stmt)
            doc = doc_res.scalar_one_or_none()
            if doc:
                print(f"Task: {t.id}\n  Doc: {doc.filename}\n  Size: {doc.file_size} bytes\n  Created: {t.created_at}\n  Updated: {t.updated_at}\n  LeaseExp: {t.lease_expires_at}\n  Stage: {t.stage}\n  Progress: {t.progress_percent}%")

if __name__ == "__main__":
    asyncio.run(check())
