"""
Level 5: Orphan Task Crash Recovery & Dead Letter Queue Handler
Scans for crashed worker tasks whose lease expired and automatically heals or routes them to DEAD_LETTER.
Fixes: P1-TASK-06, P1-TASK-07
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionLocal
from src.core.clients import InfrastructureClients
from src.core.config import settings
from src.models.db_models import Task, Document, utcnow
from src.core.tasks.outbox import TaskOutboxService
from src.core.logger import get_logger

logger = get_logger(__name__)


class OrphanTaskRecoveryScanner:
    """
    Periodic scanner that detects expired RUNNING tasks from crashed workers,
    re-enqueues them with exponential backoff, or moves exhausted tasks to DEAD_LETTER.
    """

    @classmethod
    async def scan_and_recover(
        cls,
        max_attempts: int = 3,
        session: Optional[AsyncSession] = None
    ) -> Dict[str, Any]:
        """
        Executes a single sweep of orphan task recovery.
        Can run within an existing session or open its own session.
        """
        if session is not None:
            return await cls._execute_recovery(session, max_attempts)
        else:
            async with AsyncSessionLocal() as sess:
                return await cls._execute_recovery(sess, max_attempts)

    @classmethod
    async def _execute_recovery(cls, session: AsyncSession, max_attempts: int) -> Dict[str, Any]:
        now = utcnow()
        stmt = (
            select(Task)
            .where(
                Task.status == "RUNNING",
                Task.lease_expires_at != None,
                Task.lease_expires_at < now
            )
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        orphans = result.scalars().all()

        recovered_count = 0
        dead_letter_count = 0

        for task in orphans:
            previous_attempt = task.attempt
            new_attempt = previous_attempt + 1
            new_fencing_token = task.fencing_token + 1

            if new_attempt < max_attempts:
                # 1. Eligible for Retry -> Exponential Backoff
                backoff_seconds = min(300, (2 ** new_attempt) * 5)
                new_lease = now + timedelta(seconds=backoff_seconds)

                task.status = "RETRY_WAITING"
                task.attempt = new_attempt
                task.fencing_token = new_fencing_token
                # Keep the retry deadline visible through the legacy lease
                # field as well as the explicit next_attempt_at field. The
                # scanner only considers RUNNING tasks, so this cannot be
                # mistaken for an active worker lease, while older clients
                # still see the expected retry timestamp.
                task.lease_expires_at = new_lease
                task.next_attempt_at = new_lease
                task.worker_id = None
                task.error_msg = f"Worker heartbeat timeout on attempt {previous_attempt}. Re-queued with backoff."
                task.updated_at = now

                doc = (
                    await session.execute(
                        select(Document).where(
                            Document.id == task.doc_id,
                            Document.tenant_id == task.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if doc:
                    await TaskOutboxService.enqueue(
                        session,
                        event_key=f"{task.id}:recovery:{new_attempt}",
                        task_id=task.id,
                        tenant_id=task.tenant_id,
                        event_type="DOCUMENT_RECOVERY",
                        payload={
                            "task_id": task.id,
                            "doc_id": task.doc_id,
                            "tenant_id": task.tenant_id,
                            "kb_id": task.kb_id,
                            "minio_bucket": doc.minio_bucket,
                            "minio_key": doc.minio_key,
                            "filename": doc.filename,
                            "options_json": __import__("json").dumps(task.task_options or {}, ensure_ascii=False),
                        },
                        available_at=new_lease,
                    )

                recovered_count += 1
            else:
                # 2. Exceeded Max Attempts -> DEAD_LETTER Terminal State
                task.status = "DEAD_LETTER"
                task.attempt = new_attempt
                task.fencing_token = new_fencing_token
                task.worker_id = None
                task.lease_expires_at = None
                task.next_attempt_at = None
                task.error_msg = (
                    f"Task exceeded max attempts ({max_attempts}) due to persistent worker lease timeouts."
                )
                task.updated_at = now
                dead_letter_count += 1

        await session.commit()

        return {
            "scanned_orphans": len(orphans),
            "recovered_count": recovered_count,
            "dead_letter_count": dead_letter_count,
            "timestamp": now.isoformat()
        }
