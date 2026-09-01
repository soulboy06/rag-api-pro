"""
Level 5: Task Service with FSM CAS State Validation & Fencing Token Control
Manages task queries, cursor pagination, and atomic CAS state transitions with strict validation.
Fixes: P0-REL-01, P1-TASK-01, P1-TASK-02, P1-TASK-03, P1-TASK-04, P1-TASK-08, P2-STORE-05
"""
from typing import Optional, List, Tuple, Dict, Any
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, and_, or_, desc

from src.models.db_models import Task, Document, utcnow
from src.models.schemas import TaskInfo
from src.models.fsm import TaskFSM
from src.core.exceptions import ResourceNotFoundError, InvalidStateTransitionError
from src.core.logger import get_logger

logger = get_logger(__name__)


class TaskService:
    @staticmethod
    async def get_task(
        db: AsyncSession,
        task_id: str,
        tenant_id: str
    ) -> TaskInfo:
        stmt = select(Task).where(
            Task.id == task_id,
            Task.tenant_id == tenant_id
        )
        res = await db.execute(stmt)
        task = res.scalar_one_or_none()
        if not task:
            raise ResourceNotFoundError(f"Task '{task_id}' not found")

        return TaskInfo(
            id=task.id,
            tenant_id=task.tenant_id,
            kb_id=task.kb_id,
            doc_id=task.doc_id,
            task_type=task.task_type,
            status=task.status,
            stage=task.stage or "INIT",
            progress_percent=task.progress_percent or 0,
            attempt=task.attempt or 0,
            retry_count=task.retry_count or 0,
            fencing_token=task.fencing_token or 1,
            idempotency_key=task.idempotency_key,
            worker_id=task.worker_id,
            error_msg=task.error_msg,
            created_at=task.created_at,
            updated_at=task.updated_at,
            next_attempt_at=task.next_attempt_at,
            target_generation=task.target_generation or 1,
        )

    @staticmethod
    async def acquire_task_lease(
        db: AsyncSession,
        task_id: str,
        worker_id: str,
        lease_seconds: float = 30.0
    ) -> Tuple[bool, Optional[Task]]:
        """
        Attempts to acquire a lease on a PENDING or RETRY_WAITING task.
        Increments fencing_token and transitions to RUNNING atomically.
        """
        now = utcnow()
        stmt = (
            select(Task)
            .where(
                Task.id == task_id,
                or_(
                    Task.status == "PENDING",
                    and_(
                        Task.status == "RETRY_WAITING",
                        or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= now),
                    ),
                )
            )
            .with_for_update()
        )
        result = await db.execute(stmt)
        task = result.scalar_one_or_none()
        if not task:
            return False, None

        # Atomic transition to RUNNING with lease
        task.status = "RUNNING"
        task.worker_id = worker_id
        task.attempt = (task.attempt or 0) + 1
        task.fencing_token = (task.fencing_token or 0) + 1
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        task.next_attempt_at = None
        task.updated_at = now
        await db.commit()
        return True, task

    @staticmethod
    async def update_task_status_cas(
        db: AsyncSession,
        task_id: str,
        from_status: str,
        to_status: str,
        expected_fencing_token: int,
        stage: Optional[str] = None,
        progress_percent: Optional[int] = None,
        worker_id: Optional[str] = None,
        error_msg: Optional[str] = None
    ) -> bool:
        """
        Strict CAS update: verifies both status and fencing_token before committing state change.
        Prevents zombie workers with outdated tokens from overwriting newer state.
        """
        # 1. Enforce FSM validation
        TaskFSM.validate_transition(task_id, from_status, to_status)

        now = utcnow()
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == from_status,
                Task.fencing_token == expected_fencing_token
            )
        )
        if worker_id is not None:
            stmt = stmt.where(Task.worker_id == worker_id)

        values: Dict[str, Any] = {
            "status": to_status,
            "updated_at": now,
        }
        if stage:
            values["stage"] = stage
        if progress_percent is not None:
            values["progress_percent"] = progress_percent
        if worker_id:
            values["worker_id"] = worker_id
        if error_msg is not None:
            values["error_msg"] = error_msg

        # If reaching a terminal state, clear lease
        if TaskFSM.is_terminal(to_status):
            values["lease_expires_at"] = None
            values["worker_id"] = None

        stmt = stmt.values(**values)
        res = await db.execute(stmt)
        await db.commit()
        return res.rowcount > 0

    @staticmethod
    async def complete_task_with_generation(
        db: AsyncSession,
        task_id: str,
        tenant_id: str,
        doc_id: str,
        expected_fencing_token: int,
        worker_id: str,
        generation: int,
        final_status: str = "SUCCEEDED",
        publish_generation: bool = True,
    ) -> bool:
        """Atomically finalize a task and optionally publish its generation.

        Derived stores are written before this method is called.  Until this
        PostgreSQL transaction commits, retrieval continues using the old
        document generation.  A stale worker therefore cannot make a partial
        re-ingestion visible.
        """
        if final_status not in {"SUCCEEDED", "PARTIAL_SUCCEEDED"}:
            raise InvalidStateTransitionError(
                f"Unsupported generation completion state: {final_status}"
            )
        TaskFSM.validate_transition(task_id, "RUNNING", final_status)
        now = utcnow()
        task_result = await db.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
                Task.doc_id == doc_id,
                Task.status == "RUNNING",
                Task.worker_id == worker_id,
                Task.fencing_token == expected_fencing_token,
            )
            .values(
                status=final_status,
                stage="COMPLETED",
                progress_percent=100,
                worker_id=None,
                lease_expires_at=None,
                next_attempt_at=None,
                updated_at=now,
            )
        )
        if task_result.rowcount != 1:
            await db.rollback()
            return False

        if publish_generation:
            document_result = await db.execute(
                update(Document)
                .where(
                    Document.id == doc_id,
                    Document.tenant_id == tenant_id,
                    Document.active_generation <= generation,
                )
                .values(active_generation=generation)
            )
            if document_result.rowcount != 1:
                await db.rollback()
                return False

        await db.commit()
        return True

    @staticmethod
    async def update_task_progress_cas(
        db: AsyncSession,
        task_id: str,
        worker_id: str,
        expected_fencing_token: int,
        stage: str,
        progress_percent: int,
    ) -> bool:
        """Updates progress only while the caller still owns the task lease."""
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == "RUNNING",
                Task.worker_id == worker_id,
                Task.fencing_token == expected_fencing_token,
            )
            .values(
                stage=stage,
                progress_percent=max(0, min(100, progress_percent)),
                updated_at=utcnow(),
            )
        )
        result = await db.execute(stmt)
        await db.commit()
        return result.rowcount > 0

    @staticmethod
    async def list_tasks_cursor(
        db: AsyncSession,
        tenant_id: str,
        kb_id: Optional[str] = None,
        cursor_created_at: Optional[datetime] = None,
        limit: int = 20
    ) -> List[TaskInfo]:
        """Cursor-based pagination for listing tenant tasks efficiently."""
        query = select(Task).where(Task.tenant_id == tenant_id)
        if kb_id:
            query = query.where(Task.kb_id == kb_id)
        if cursor_created_at:
            query = query.where(Task.created_at < cursor_created_at)

        query = query.order_by(desc(Task.created_at)).limit(limit)
        result = await db.execute(query)
        tasks = result.scalars().all()

        return [
            TaskInfo(
                id=t.id,
                tenant_id=t.tenant_id,
                kb_id=t.kb_id,
                doc_id=t.doc_id,
                task_type=t.task_type,
                status=t.status,
                stage=t.stage or "INIT",
                progress_percent=t.progress_percent or 0,
                attempt=t.attempt or 0,
                retry_count=t.retry_count or 0,
                fencing_token=t.fencing_token or 1,
                idempotency_key=t.idempotency_key,
                worker_id=t.worker_id,
                error_msg=t.error_msg,
                created_at=t.created_at,
                updated_at=t.updated_at,
                next_attempt_at=t.next_attempt_at,
                target_generation=t.target_generation or 1,
            )
            for t in tasks
        ]

    @staticmethod
    async def retry_task(
        db: AsyncSession,
        task_id: str,
        tenant_id: str
    ) -> TaskInfo:
        """Manually re-enqueues a FAILED or DEAD_LETTER task."""
        stmt = select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id).with_for_update()
        res = await db.execute(stmt)
        task = res.scalar_one_or_none()
        if not task:
            raise ResourceNotFoundError(f"Task '{task_id}' not found")
        if task.status not in ["FAILED", "DEAD_LETTER"]:
            raise InvalidStateTransitionError(f"Cannot retry task in '{task.status}' state")

        from src.models.db_models import Document
        from src.core.tasks.outbox import TaskOutboxService
        import json

        doc_stmt = select(Document).where(
            Document.id == task.doc_id,
            Document.tenant_id == tenant_id,
        )
        doc = (await db.execute(doc_stmt)).scalar_one_or_none()
        if not doc:
            raise ResourceNotFoundError(f"Document '{task.doc_id}' not found")

        now = utcnow()
        next_retry_count = (task.retry_count or 0) + 1
        task.status = "PENDING"
        task.stage = "RETRY_QUEUED"
        task.retry_count = next_retry_count
        task.fencing_token = (task.fencing_token or 0) + 1
        task.worker_id = None
        task.lease_expires_at = None
        task.next_attempt_at = None
        task.error_msg = None
        task.updated_at = now
        await TaskOutboxService.enqueue(
            db,
            event_key=f"{task.id}:retry:{next_retry_count}",
            task_id=task.id,
            tenant_id=task.tenant_id,
            event_type="DOCUMENT_RETRY",
            payload={
                "task_id": task.id,
                "tenant_id": task.tenant_id,
                "kb_id": task.kb_id,
                "doc_id": task.doc_id,
                "minio_bucket": doc.minio_bucket,
                "minio_key": doc.minio_key,
                "filename": doc.filename,
                "options_json": json.dumps(task.task_options or {}, ensure_ascii=False),
            },
        )
        await db.commit()
        try:
            await TaskOutboxService.publish_pending(task_id=task.id)
        except Exception as publish_err:
            # The task and its outbox event are already committed. Delivery
            # will be retried by the worker's periodic publisher sweep.
            logger.warning("Task retry outbox publication deferred", extra={"error": str(publish_err)})

        return await TaskService.get_task(db, task.id, tenant_id)
