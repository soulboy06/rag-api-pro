"""Durable PostgreSQL outbox and idempotent Redis Stream publisher."""
import asyncio
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients import InfrastructureClients
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.models.db_models import TaskOutboxEvent, utcnow


class TaskOutboxService:
    """Publishes task events without coupling a business commit to Redis availability."""

    @staticmethod
    async def enqueue(
        session: AsyncSession,
        *,
        event_key: str,
        task_id: str,
        tenant_id: str,
        event_type: str,
        payload: Dict[str, Any],
        available_at=None,
    ) -> TaskOutboxEvent:
        # Flush the business aggregate before checking/inserting the event.
        # This preserves the FK invariant when callers add a new Task and
        # its outbox event in the same SQLAlchemy transaction.
        await session.flush()
        existing = (
            await session.execute(
                select(TaskOutboxEvent).where(TaskOutboxEvent.event_key == event_key)
            )
        ).scalar_one_or_none()
        if existing:
            return existing

        event = TaskOutboxEvent(
            id=str(uuid.uuid4()),
            event_key=event_key,
            task_id=task_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
            status="PENDING",
            available_at=available_at or utcnow(),
        )
        session.add(event)
        return event

    @classmethod
    async def publish_pending(
        cls,
        limit: int = 20,
        task_id: Optional[str] = None,
    ) -> int:
        """Claims and publishes due events; failed events remain retryable."""
        published = 0
        for _ in range(limit):
            async with AsyncSessionLocal() as session:
                now = utcnow()
                conditions = [
                    or_(
                        TaskOutboxEvent.status == "PENDING",
                        and_(
                            TaskOutboxEvent.status == "PUBLISHING",
                            TaskOutboxEvent.lease_expires_at < now,
                        ),
                    ),
                    or_(
                        TaskOutboxEvent.available_at.is_(None),
                        TaskOutboxEvent.available_at <= now,
                    ),
                ]
                if task_id:
                    conditions.append(TaskOutboxEvent.task_id == task_id)

                event = (
                    await session.execute(
                        select(TaskOutboxEvent)
                        .where(and_(*conditions))
                        .order_by(TaskOutboxEvent.created_at.asc())
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if not event:
                    break

                event.status = "PUBLISHING"
                event.lease_expires_at = now + timedelta(seconds=60)
                await session.commit()
                event_id = event.id
                payload = dict(event.payload)

            try:
                redis_client = InfrastructureClients.get_redis()
                fields = {
                    key: str(value) if value is not None else ""
                    for key, value in payload.items()
                    if not isinstance(value, (dict, list))
                }
                fields["outbox_event_id"] = event_id
                await redis_client.xadd(
                    name=settings.REDIS_STREAM_NAME,
                    fields=fields,
                )
            except Exception as exc:
                async with AsyncSessionLocal() as session:
                    current = (
                        await session.execute(
                            select(TaskOutboxEvent).where(TaskOutboxEvent.id == event_id)
                        )
                    ).scalar_one_or_none()
                    if current:
                        attempts = current.attempts + 1
                        current.status = "PENDING"
                        current.attempts = attempts
                        current.lease_expires_at = None
                        current.available_at = utcnow() + timedelta(
                            seconds=min(300, 2 ** min(attempts, 8))
                        )
                        current.last_error = str(exc)[:1000]
                        await session.commit()
                continue

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    update(TaskOutboxEvent)
                    .where(
                        TaskOutboxEvent.id == event_id,
                        TaskOutboxEvent.status == "PUBLISHING",
                    )
                    .values(
                        status="PUBLISHED",
                        published_at=utcnow(),
                        lease_expires_at=None,
                        last_error=None,
                    )
                )
                await session.commit()
                if result.rowcount:
                    published += 1

        return published
