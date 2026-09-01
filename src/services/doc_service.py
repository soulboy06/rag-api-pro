"""
Level 1: Document Service with Magic Number Inspection and Stream Truncation
Handles safe file streaming to MinIO, PostgreSQL metadata persistence, and Redis Streams enqueueing.
Fixes: P0-SEC-02, P0-SEC-03, P1-API-07, P1-API-08, P1-STORE-04, P1-STORE-05
"""
import io
import uuid
import json
import asyncio
from typing import List, Optional
from datetime import datetime, timezone
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
from qdrant_client.http import models as qmodels

from src.core.config import settings
from src.core.clients import InfrastructureClients
from src.models.db_models import Document, Task, KnowledgeBase, Tenant
from src.models.schemas import DocumentInfo, TaskInfo, UploadResponse
from src.core.security.file_validator import FileValidator
from src.core.tasks.idempotency import IdempotencyEngine
from src.core.tasks.outbox import TaskOutboxService
from src.core.exceptions import ResourceNotFoundError, StorageUnavailableError, SecurityViolationError, QuotaExceededError
from src.core.tenant.config_manager import TenantConfigManager
from src.core.logger import get_logger

logger = get_logger(__name__)


class DocService:
    @staticmethod
    def derive_index_status(
        task_status: Optional[str],
        indexed_chunks: Optional[int],
    ) -> tuple[str, Optional[int]]:
        """Derive user-facing search readiness from task and index evidence.

        PostgreSQL task status describes what the worker reported in the past;
        Qdrant count describes what is actually searchable now.  A successful
        task with zero current chunks therefore cannot be presented as ready.
        """
        normalized = (task_status or "").upper()
        if normalized in {"PENDING", "RUNNING", "RETRY_WAITING"}:
            return "PROCESSING", indexed_chunks

        if indexed_chunks is None:
            return (
                "FAILED" if normalized in {"FAILED", "DEAD_LETTER"} else "UNKNOWN",
                None,
            )

        if indexed_chunks <= 0:
            return "NOT_INDEXED", 0
        if normalized == "PARTIAL_SUCCEEDED":
            return "PARTIAL", indexed_chunks
        return "READY", indexed_chunks

    @staticmethod
    async def _count_indexed_chunks(document: Document) -> Optional[int]:
        """Count this document's active-generation chunks in Qdrant.

        Listing documents must remain available during a vector-store outage,
        so an unavailable count is surfaced as UNKNOWN rather than silently
        converted into a false success.
        """
        try:
            qdrant = InfrastructureClients.get_qdrant()
            generation = document.active_generation or 1
            must = [
                qmodels.FieldCondition(
                    key="tenant_id",
                    match=qmodels.MatchValue(value=document.tenant_id),
                ),
                qmodels.FieldCondition(
                    key="kb_id",
                    match=qmodels.MatchValue(value=document.kb_id),
                ),
                qmodels.FieldCondition(
                    key="doc_id",
                    match=qmodels.MatchValue(value=document.id),
                ),
            ]
            if generation == 1:
                generation_conditions = [
                    qmodels.FieldCondition(
                        key="generation",
                        match=qmodels.MatchValue(value=generation),
                    ),
                    qmodels.IsEmptyCondition(
                        is_empty=qmodels.PayloadField(key="generation")
                    ),
                ]
                count_filter = qmodels.Filter(
                    must=must,
                    should=generation_conditions,
                    min_should=qmodels.MinShould(
                        conditions=generation_conditions,
                        min_count=1,
                    ),
                )
            else:
                count_filter = qmodels.Filter(
                    must=must + [
                        qmodels.FieldCondition(
                            key="generation",
                            match=qmodels.MatchValue(value=generation),
                        )
                    ]
                )
            result = await qdrant.count(
                collection_name=settings.QDRANT_COLLECTION,
                count_filter=count_filter,
                exact=True,
            )
            return int(result.count)
        except Exception as exc:
            logger.warning(
                "Unable to verify document index readiness",
                extra={"doc_id": document.id, "error": str(exc)},
            )
            return None

    @staticmethod
    def _existing_upload_response(
        existing_doc: Document,
        existing_task: Task,
        reason: str,
    ) -> UploadResponse:
        """Builds the idempotent response shared by both race-check paths."""
        return UploadResponse(
            document=DocumentInfo(
                id=existing_doc.id,
                tenant_id=existing_doc.tenant_id,
                kb_id=existing_doc.kb_id,
                filename=existing_doc.filename,
                content_type=existing_doc.content_type,
                file_size=existing_doc.file_size,
                content_hash=existing_doc.content_hash,
                task_status=existing_task.status,
                task_stage=existing_task.stage,
                task_progress=existing_task.progress_percent,
                task_id=existing_task.id,
                active_generation=existing_doc.active_generation or 1,
                index_status=DocService.derive_index_status(existing_task.status, None)[0],
                indexed_chunks=None,
                created_at=existing_doc.created_at,
            ),
            task=TaskInfo(
                id=existing_task.id,
                tenant_id=existing_task.tenant_id,
                kb_id=existing_task.kb_id,
                doc_id=existing_task.doc_id,
                task_type=existing_task.task_type,
                status=existing_task.status,
                stage=existing_task.stage,
                progress_percent=existing_task.progress_percent,
                attempt=existing_task.attempt,
                retry_count=existing_task.retry_count,
                fencing_token=existing_task.fencing_token,
                idempotency_key=existing_task.idempotency_key,
                worker_id=existing_task.worker_id,
                error_msg=existing_task.error_msg,
                created_at=existing_task.created_at,
                updated_at=existing_task.updated_at,
                next_attempt_at=existing_task.next_attempt_at,
                target_generation=existing_task.target_generation or 1,
            ),
            message=f"Document already uploaded ({reason}). Returning existing record.",
        )

    @staticmethod
    async def upload_document(
        db: AsyncSession,
        file: UploadFile,
        tenant_id: str,
        kb_id: str,
    ) -> UploadResponse:
        # 1. Verify Knowledge Base exists and belongs to tenant
        kb_stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.tenant_id == tenant_id
        )
        kb_res = await db.execute(kb_stmt)
        kb = kb_res.scalar_one_or_none()
        if not kb:
            raise ResourceNotFoundError(f"Knowledge Base '{kb_id}' not found for tenant")

        # 2. Load the durable tenant policy before reading the upload. The
        # policy controls both per-file and aggregate storage limits.
        tenant_config = await TenantConfigManager.load_latest_config_db(db, tenant_id)
        file_bytes, content_hash, file_size = await FileValidator.validate_and_read_stream(
            file,
            max_size_bytes=tenant_config.max_file_size_bytes,
        )
        filename = file.filename or "unnamed_document.txt"
        content_type = file.content_type or "application/octet-stream"

        # 2.1 Level 5 Idempotency Pre-Check
        idempotency_key = IdempotencyEngine.compute_fingerprint(tenant_id, kb_id, filename, content_hash)
        is_dup, existing_task, reason = await IdempotencyEngine.check_idempotent_task(
            session=db,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key
        )
        if is_dup and existing_task:
            doc_stmt = select(Document).where(Document.id == existing_task.doc_id)
            doc_res = await db.execute(doc_stmt)
            existing_doc = doc_res.scalar_one_or_none()
            if existing_doc:
                return DocService._existing_upload_response(existing_doc, existing_task, reason)

        # Serialize quota checks per tenant. Without the row lock, two
        # concurrent uploads could each pass the aggregate storage/task limit.
        tenant_row = (
            await db.execute(
                select(Tenant).where(Tenant.id == tenant_id).with_for_update()
            )
        ).scalar_one_or_none()
        if not tenant_row:
            raise ResourceNotFoundError(f"Tenant '{tenant_id}' not found")

        # The first idempotency check may have run before another concurrent
        # upload committed. Re-check after the tenant row lock so the unique
        # task key becomes a deterministic response instead of an IntegrityError.
        is_dup, existing_task, reason = await IdempotencyEngine.check_idempotent_task(
            session=db,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        )
        if is_dup and existing_task:
            existing_doc = (
                await db.execute(
                    select(Document).where(Document.id == existing_task.doc_id)
                )
            ).scalar_one_or_none()
            if existing_doc:
                return DocService._existing_upload_response(existing_doc, existing_task, reason)

        storage_used = (
            await db.execute(
                select(func.coalesce(func.sum(Document.file_size), 0)).where(
                    Document.tenant_id == tenant_id
                )
            )
        ).scalar_one()
        document_count = (
            await db.execute(
                select(func.count(Document.id)).where(Document.tenant_id == tenant_id)
            )
        ).scalar_one()
        active_task_count = (
            await db.execute(
                select(func.count(Task.id)).where(
                    Task.tenant_id == tenant_id,
                    Task.status.in_(("PENDING", "RUNNING", "RETRY_WAITING")),
                )
            )
        ).scalar_one()
        if file_size > tenant_config.max_file_size_bytes:
            raise QuotaExceededError("Uploaded file exceeds the tenant file-size quota")
        if int(storage_used or 0) + file_size > tenant_config.max_storage_bytes:
            raise QuotaExceededError("Tenant storage quota exceeded")
        if int(document_count or 0) >= tenant_config.max_documents:
            raise QuotaExceededError("Tenant document-count quota exceeded")
        if int(active_task_count or 0) >= tenant_config.max_active_tasks:
            raise QuotaExceededError("Tenant active-task quota exceeded")

        # 3. Upload to MinIO
        minio_client = InfrastructureClients.get_minio()
        doc_id = str(uuid.uuid4())
        minio_key = f"{tenant_id}/{kb_id}/{doc_id}/{filename}"
        
        try:
            await asyncio.to_thread(
                minio_client.put_object,
                bucket_name=settings.MINIO_BUCKET_NAME,
                object_name=minio_key,
                data=io.BytesIO(file_bytes),
                length=file_size,
                content_type=content_type,
            )
        except Exception as e:
            logger.error("Failed to upload file to object storage", extra={"error": str(e)})
            raise StorageUnavailableError("Failed to persist uploaded file") from e

        # 4. Save Document metadata in PostgreSQL
        doc = Document(
            id=doc_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            filename=filename,
            content_type=content_type,
            file_size=file_size,
            minio_bucket=settings.MINIO_BUCKET_NAME,
            minio_key=minio_key,
            content_hash=content_hash,
        )
        db.add(doc)

        # 5. Create Task in PostgreSQL (PENDING) with idempotency_key
        task_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            doc_id=doc_id,
            task_type="DOCUMENT_INGESTION",
            status="PENDING",
            stage="INIT",
            attempt=0,
            idempotency_key=idempotency_key,
        )
        db.add(task)
        await TaskOutboxService.enqueue(
            db,
            event_key=task_id,
            task_id=task_id,
            tenant_id=tenant_id,
            event_type="DOCUMENT_INGESTION",
            payload={
                "task_id": task_id,
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "doc_id": doc_id,
                "minio_bucket": settings.MINIO_BUCKET_NAME,
                "minio_key": minio_key,
                "filename": filename,
                "content_hash": content_hash,
                "options_json": json.dumps({}, ensure_ascii=False),
            },
        )
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            try:
                InfrastructureClients.delete_minio_object(
                    object_name=minio_key,
                    bucket_name=settings.MINIO_BUCKET_NAME,
                )
            except Exception as cleanup_err:
                logger.error("Failed to compensate uploaded object after metadata failure", extra={"error": str(cleanup_err)})
            raise
        await db.refresh(doc)
        await db.refresh(task)

        # 6. Best-effort publish. The durable outbox remains if Redis is unavailable.
        try:
            await TaskOutboxService.publish_pending(task_id=task_id)
        except Exception as publish_err:
            # PostgreSQL metadata and the outbox are already durable. The
            # publisher will retry later; do not turn a successful upload into
            # a misleading 500 merely because Redis is temporarily down.
            logger.warning("Task outbox publication deferred", extra={"error": str(publish_err)})

        return UploadResponse(
            document=DocumentInfo(
                id=doc.id,
                tenant_id=doc.tenant_id,
                kb_id=doc.kb_id,
                filename=doc.filename,
                content_type=doc.content_type,
                file_size=doc.file_size,
                content_hash=doc.content_hash,
                active_generation=doc.active_generation or 1,
                index_status=DocService.derive_index_status(task.status, None)[0],
                indexed_chunks=None,
                created_at=doc.created_at,
            ),
            task=TaskInfo(
                id=task.id,
                tenant_id=task.tenant_id,
                kb_id=task.kb_id,
                doc_id=task.doc_id,
                task_type=task.task_type,
                status=task.status,
                stage=task.stage,
                progress_percent=task.progress_percent,
                attempt=task.attempt,
                target_generation=task.target_generation or 1,
                next_attempt_at=task.next_attempt_at,
                error_msg=task.error_msg,
                created_at=task.created_at,
                updated_at=task.updated_at,
            ),
            message="Document uploaded successfully and queued for ingestion"
        )

    @staticmethod
    async def get_download_url(
        db: AsyncSession,
        doc_id: str,
        tenant_id: str,
        expires_seconds: int = 3600
    ) -> str:
        """Generates secure temporary presigned download URL."""
        stmt = select(Document).where(
            Document.id == doc_id,
            Document.tenant_id == tenant_id
        )
        res = await db.execute(stmt)
        doc = res.scalar_one_or_none()
        if not doc:
            raise ResourceNotFoundError(f"Document '{doc_id}' not found")

        return InfrastructureClients.get_presigned_download_url(
            object_name=doc.minio_key,
            bucket_name=doc.minio_bucket,
            expires_seconds=expires_seconds,
        )

    @staticmethod
    async def delete_document(
        db: AsyncSession,
        doc_id: str,
        tenant_id: str
    ) -> bool:
        """Safely removes document from PostgreSQL, MinIO, Qdrant vectors, and Memgraph graph relationships."""
        from qdrant_client.http import models as qmodels

        stmt = select(Document).where(
            Document.id == doc_id,
            Document.tenant_id == tenant_id
        )
        res = await db.execute(stmt)
        doc = res.scalar_one_or_none()
        if not doc:
            raise ResourceNotFoundError(f"Document '{doc_id}' not found")

        cleanup_errors = []

        # 1. Delete MinIO object.  A failed external cleanup must prevent the
        # metadata row from disappearing, otherwise reconciliation cannot know
        # that the physical object still needs cleanup.
        try:
            removed = await asyncio.to_thread(
                InfrastructureClients.delete_minio_object,
                object_name=doc.minio_key,
                bucket_name=doc.minio_bucket,
            )
            if not removed:
                raise RuntimeError("object removal returned false")
        except Exception as exc:
            cleanup_errors.append(f"MinIO: {exc}")

        # 2. Delete Qdrant vectors
        try:
            qdrant = InfrastructureClients.get_qdrant()
            await qdrant.delete(
                collection_name=settings.QDRANT_COLLECTION,
                points_selector=qmodels.FilterSelector(
                    filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(key="tenant_id", match=qmodels.MatchValue(value=tenant_id)),
                            qmodels.FieldCondition(key="doc_id", match=qmodels.MatchValue(value=doc_id)),
                        ]
                    )
                )
            )
        except Exception as exc:
            cleanup_errors.append(f"Qdrant: {exc}")

        # 3. Delete Memgraph relationships and only the document-owned
        # entities.  Ingestion stores document ownership on relationships;
        # matching Entity.doc_id alone misses the normal graph shape.
        try:
            memgraph = InfrastructureClients.get_memgraph()
            async with memgraph.session() as session:
                relationship_cleanup = """
                MATCH (source)-[r:RELATED_TO {doc_id: $doc_id}]-()
                WHERE coalesce(r.tenant_id, source.tenant_id) = $tenant_id
                DELETE r;
                """
                await session.run(
                    relationship_cleanup,
                    doc_id=doc_id,
                    tenant_id=tenant_id,
                )

                entity_cleanup = """
                MATCH (e:Entity {tenant_id: $tenant_id, kb_id: $kb_id})
                WHERE e.doc_id = $doc_id
                DETACH DELETE e;
                """
                await session.run(
                    entity_cleanup,
                    tenant_id=tenant_id,
                    kb_id=doc.kb_id,
                    doc_id=doc_id,
                )
                # The ingestion graph intentionally shares entity nodes
                # between documents. Remove only now-isolated nodes in this
                # tenant/KB; never delete another document's shared entity.
                await session.run(
                    """
                    MATCH (e:Entity {tenant_id: $tenant_id, kb_id: $kb_id})
                    WHERE NOT exists((e)--())
                    DETACH DELETE e;
                    """,
                    tenant_id=tenant_id,
                    kb_id=doc.kb_id,
                )
        except Exception as exc:
            cleanup_errors.append(f"Memgraph: {exc}")

        if cleanup_errors:
            logger.error(
                "Document cleanup incomplete; metadata was retained; errors=%s",
                cleanup_errors,
                extra={"doc_id": doc_id, "cleanup_errors": cleanup_errors},
            )
            raise StorageUnavailableError(
                "Document cleanup incomplete; the metadata was retained for retry"
            )

        # 4. Delete from PostgreSQL (cascades tasks)
        await db.delete(doc)
        await db.commit()
        return True

    @staticmethod
    async def reingest_document(
        db: AsyncSession,
        doc_id: str,
        tenant_id: str,
        force_parser: Optional[str] = None
    ) -> TaskInfo:
        """Creates a re-ingestion task for an existing document."""
        from src.services.task_service import TaskService
        from src.models.db_models import utcnow

        stmt = (
            select(Document)
            .where(Document.id == doc_id, Document.tenant_id == tenant_id)
            .with_for_update()
        )
        res = await db.execute(stmt)
        doc = res.scalar_one_or_none()
        if not doc:
            raise ResourceNotFoundError(f"Document '{doc_id}' not found")

        tenant_row = (
            await db.execute(
                select(Tenant).where(Tenant.id == tenant_id).with_for_update()
            )
        ).scalar_one_or_none()
        if not tenant_row:
            raise ResourceNotFoundError(f"Tenant '{tenant_id}' not found")
        tenant_config = await TenantConfigManager.load_latest_config_db(db, tenant_id)
        active_task_count = (
            await db.execute(
                select(func.count(Task.id)).where(
                    Task.tenant_id == tenant_id,
                    Task.status.in_(("PENDING", "RUNNING", "RETRY_WAITING")),
                )
            )
        ).scalar_one()
        if int(active_task_count or 0) >= tenant_config.max_active_tasks:
            raise QuotaExceededError("Tenant active-task quota exceeded")

        new_task = Task(
            id=str(uuid.uuid4()),
            tenant_id=doc.tenant_id,
            kb_id=doc.kb_id,
            doc_id=doc.id,
            task_type="DOCUMENT_REINGESTION",
            status="PENDING",
            stage="INIT",
            progress_percent=0,
            attempt=0,
            retry_count=0,
            fencing_token=1,
            idempotency_key=f"reingest_{doc.id}_{uuid.uuid4().hex}",
            task_options={"force_parser": force_parser} if force_parser else {},
            target_generation=(doc.active_generation or 1) + 1,
            created_at=utcnow(),
            updated_at=utcnow()
        )
        new_task.task_options = {
            **(new_task.task_options or {}),
            "target_generation": new_task.target_generation,
        }
        db.add(new_task)
        await TaskOutboxService.enqueue(
            db,
            event_key=new_task.id,
            task_id=new_task.id,
            tenant_id=new_task.tenant_id,
            event_type="DOCUMENT_REINGESTION",
            payload={
                "task_id": new_task.id,
                "tenant_id": new_task.tenant_id,
                "kb_id": new_task.kb_id,
                "doc_id": new_task.doc_id,
                "minio_bucket": doc.minio_bucket,
                "minio_key": doc.minio_key,
                "filename": doc.filename,
                "options_json": json.dumps(new_task.task_options or {}, ensure_ascii=False),
            },
        )
        await db.commit()
        await db.refresh(new_task)

        try:
            await TaskOutboxService.publish_pending(task_id=new_task.id)
        except Exception as publish_err:
            logger.warning("Re-ingestion outbox publication deferred", extra={"error": str(publish_err)})

        return await TaskService.get_task(db, new_task.id, tenant_id)


    @staticmethod
    async def list_documents(
        db: AsyncSession,
        tenant_id: str,
        kb_id: Optional[str] = None
    ) -> List[DocumentInfo]:
        stmt = select(Document).where(Document.tenant_id == tenant_id)
        if kb_id:
            stmt = stmt.where(Document.kb_id == kb_id)
        stmt = stmt.order_by(Document.created_at.desc())
        res = await db.execute(stmt)
        docs = res.scalars().all()

        doc_ids = [d.id for d in docs]
        task_map = {}
        if doc_ids:
            task_stmt = select(Task).where(Task.doc_id.in_(doc_ids)).order_by(Task.created_at.desc())
            task_res = await db.execute(task_stmt)
            tasks = task_res.scalars().all()
            for t in tasks:
                if t.doc_id not in task_map:
                    task_map[t.doc_id] = t

        results = []
        for d in docs:
            t = task_map.get(d.id)
            task_status = t.status if t else "SUCCEEDED"
            indexed_chunks = await DocService._count_indexed_chunks(d)
            index_status, indexed_chunks = DocService.derive_index_status(
                task_status,
                indexed_chunks,
            )
            results.append(
                DocumentInfo(
                    id=d.id,
                    tenant_id=d.tenant_id,
                    kb_id=d.kb_id,
                    filename=d.filename,
                    content_type=d.content_type,
                    file_size=d.file_size,
                    content_hash=d.content_hash,
                    task_status=task_status,
                    task_stage=t.stage if t else "COMPLETED",
                    task_progress=t.progress_percent if t else 100,
                    task_id=t.id if t else None,
                    error_msg=t.error_msg if t else None,
                    active_generation=d.active_generation or 1,
                    index_status=index_status,
                    indexed_chunks=indexed_chunks,
                    created_at=d.created_at,
                )
            )
        return results
