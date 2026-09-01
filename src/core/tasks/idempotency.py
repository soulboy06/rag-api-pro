"""
Level 5: Business-Level Task Idempotency Engine
Prevents duplicate ingestion, redundant vector embedding generation, and graph cluttering.
Fixes: P1-TASK-07, P1-API-09
"""
import hashlib
from typing import Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.db_models import Task, Document


class IdempotencyEngine:
    @staticmethod
    def compute_fingerprint(tenant_id: str, kb_id: str, filename: str, content_hash: str) -> str:
        """Computes a deterministic SHA-256 idempotency fingerprint."""
        raw = f"{tenant_id}:{kb_id}:{filename}:{content_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    async def check_idempotent_task(
        cls,
        session: AsyncSession,
        tenant_id: str,
        idempotency_key: str
    ) -> Tuple[bool, Optional[Task], str]:
        """
        Checks if a task with the given idempotency_key already exists.
        Returns: (is_duplicate, existing_task, action_reason)
        """
        stmt = (
            select(Task)
            .where(
                Task.tenant_id == tenant_id,
                Task.idempotency_key == idempotency_key
            )
            .order_by(Task.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        existing_task = result.scalar_one_or_none()

        if not existing_task:
            return False, None, "NEW_TASK"

        # Case 1: Already Succeeded -> Return immediately, avoid redundant work
        if existing_task.status in {"SUCCEEDED", "PARTIAL_SUCCEEDED"}:
            return True, existing_task, "ALREADY_COMPLETED"

        # Case 2: In-Flight (PENDING / RUNNING / RETRY_WAITING) -> Avoid duplicate execution
        if existing_task.status in {"PENDING", "RUNNING", "RETRY_WAITING"}:
            return True, existing_task, "IN_FLIGHT"

        # Case 3: Failed or Cancelled -> Allow retry by creating a new task
        return False, existing_task, "PREVIOUS_FAILED_ALLOW_RETRY"
