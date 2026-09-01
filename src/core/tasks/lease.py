"""
Level 5: Task Heartbeat Lease Guardian & Fencing Token Validator
Maintains background heartbeats, refreshes PostgreSQL lease timestamps, and protects against split-brain zombie workers.
Fixes: P0-REL-02, P1-STORE-07, P1-TASK-05
"""
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy import update, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionLocal
from src.models.db_models import Task, utcnow
from src.core.logger import get_logger

logger = get_logger(__name__)


class HeartbeatLeaseGuardian:
    """
    Background heartbeat coroutine that periodically refreshes the PostgreSQL lease
    timestamp while a worker is actively processing a task.
    """

    def __init__(
        self,
        task_id: str,
        worker_id: str,
        fencing_token: int,
        heartbeat_interval_seconds: float = 10.0,
        lease_duration_seconds: float = 30.0
    ):
        self.task_id = task_id
        self.worker_id = worker_id
        self.fencing_token = fencing_token
        self.heartbeat_interval = heartbeat_interval_seconds
        self.lease_duration = lease_duration_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self):
        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self):
        """Continuously refreshes lease_expires_at while fencing_token matches."""
        while self._running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                if not self._running:
                    break

                new_expiry = utcnow() + timedelta(seconds=self.lease_duration)
                async with AsyncSessionLocal() as session:
                    stmt = (
                        update(Task)
                        .where(
                            Task.id == self.task_id,
                            Task.worker_id == self.worker_id,
                            Task.fencing_token == self.fencing_token,
                            Task.status == "RUNNING"
                        )
                        .values(
                            lease_expires_at=new_expiry,
                            updated_at=utcnow()
                        )
                    )
                    result = await session.execute(stmt)
                    await session.commit()

                    if result.rowcount == 0:
                        # Fencing token or status was changed by a takeover worker! Stop heartbeat immediately.
                        logger.warning(
                            "Lease heartbeat stopped: Task ownership or status modified",
                            extra={"task_id": self.task_id, "worker_id": self.worker_id}
                        )
                        self._running = False
                        break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"Error refreshing task lease heartbeat: {str(e)}",
                    extra={"task_id": self.task_id, "worker_id": self.worker_id}
                )
