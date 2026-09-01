"""
Level 8: Cross-Storage Data Consistency Reconciliation Engine
Implements strict Single Source of Truth (PostgreSQL & MinIO) reconciliation against
secondary reconstructable index tiers (Qdrant & Memgraph).
Fixes: P1-STORE-01, P1-STORE-02, P1-STORE-03, P2-STORE-01, P2-STORE-02, P2-STORE-03, P2-STORE-04
"""
import asyncio
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime, timezone
from sqlalchemy import select
from qdrant_client.http import models as qmodels

from src.core.config import settings
from src.core.clients import InfrastructureClients
from src.core.database import AsyncSessionLocal
from src.models.db_models import Document, Task
from src.core.monitoring.metrics import prometheus_metrics
from src.core.logger import get_logger

logger = get_logger("rag.storage.reconciler")


@dataclass
class StorageDriftItem:
    storage: str          # "qdrant", "memgraph", "minio", "pg"
    drift_type: str       # "orphan_vector", "orphan_graph_edge", "orphan_file", "missing_vector"
    doc_id: str
    tenant_id: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationReport:
    timestamp: str
    tenant_filter: Optional[str]
    pg_docs_count: int = 0
    qdrant_points_count: int = 0
    minio_objects_count: int = 0
    memgraph_edges_count: int = 0
    drifts: List[StorageDriftItem] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return len(self.drifts) > 0


@dataclass
class ReconciliationResult:
    dry_run: bool
    cleaned_orphan_vectors: int = 0
    cleaned_orphan_edges: int = 0
    cleaned_orphan_files: int = 0
    repaired_documents: int = 0
    duration_seconds: float = 0.0


class StorageReconciler:
    """Enterprise Cross-Storage Consistency Auditor & Safe Cleaner."""

    @classmethod
    async def audit_consistency(cls, tenant_id: Optional[str] = None) -> ReconciliationReport:
        """
        Scans all 4 persistent storage tiers (PostgreSQL, MinIO, Qdrant, Memgraph)
        and detects any consistency drifts without modifying data.
        """
        report = ReconciliationReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            tenant_filter=tenant_id
        )

        # 1. Fetch valid Document IDs from PostgreSQL (Single Source of Truth)
        valid_pg_doc_ids: Set[str] = set()
        valid_pg_doc_pairs: Set[tuple[str, str]] = set()
        valid_object_keys: Set[str] = set()
        succeeded_doc_ids: Set[str] = set()
        doc_tenant_map: Dict[str, str] = {}

        async with AsyncSessionLocal() as session:
            stmt = select(Document)
            if tenant_id:
                stmt = stmt.where(Document.tenant_id == tenant_id)
            res = await session.execute(stmt)
            docs = res.scalars().all()
            report.pg_docs_count = len(docs)

            for d in docs:
                valid_pg_doc_ids.add(d.id)
                valid_pg_doc_pairs.add((d.tenant_id, d.id))
                doc_tenant_map[d.id] = d.tenant_id
                valid_object_keys.add(f"{d.minio_bucket}:{d.minio_key}")

            # Succeeded tasks
            t_stmt = select(Task.doc_id).where(Task.status == "SUCCEEDED")
            if tenant_id:
                t_stmt = t_stmt.where(Task.tenant_id == tenant_id)
            t_res = await session.execute(t_stmt)
            for s_row in t_res.scalars().all():
                if s_row:
                    succeeded_doc_ids.add(s_row)

        # 2. Audit Qdrant Vectors
        qdrant = InfrastructureClients.get_qdrant()
        qdrant_doc_pairs: Set[tuple[str, str]] = set()
        try:
            scroll_filter = None
            if tenant_id:
                scroll_filter = qmodels.Filter(
                    must=[qmodels.FieldCondition(key="tenant_id", match=qmodels.MatchValue(value=tenant_id))]
                )
            
            # Scroll through all points
            offset = None
            total_points = 0
            while True:
                scroll_res = await qdrant.scroll(
                    collection_name=settings.QDRANT_COLLECTION,
                    scroll_filter=scroll_filter,
                    limit=200,
                    offset=offset,
                    with_payload=True
                )
                points, next_offset = scroll_res
                total_points += len(points)
                
                for pt in points:
                    p = pt.payload or {}
                    d_id = p.get("doc_id")
                    t_id = p.get("tenant_id", "unknown")
                    if d_id:
                        qdrant_doc_pairs.add((t_id, d_id))
                        if (t_id, d_id) not in valid_pg_doc_pairs:
                            report.drifts.append(
                                StorageDriftItem(
                                    storage="qdrant",
                                    drift_type="orphan_vector",
                                    doc_id=d_id,
                                    tenant_id=t_id,
                                    details={"point_id": pt.id, "chunk_index": p.get("chunk_index")}
                                )
                            )
                if not next_offset:
                    break
                offset = next_offset
            report.qdrant_points_count = total_points

        except Exception as e:
            logger.error(f"Failed to audit Qdrant vectors: {e}")

        # Check for missing vectors for SUCCEEDED documents
        for s_id in succeeded_doc_ids:
            if not any(doc_id == s_id for _, doc_id in qdrant_doc_pairs):
                report.drifts.append(
                    StorageDriftItem(
                        storage="qdrant",
                        drift_type="missing_vector",
                        doc_id=s_id,
                        tenant_id=doc_tenant_map.get(s_id, "unknown"),
                        details={"message": "Document marked SUCCEEDED but has 0 vectors in Qdrant"}
                    )
                )

        # 3. Audit Memgraph Graph Edges
        memgraph = InfrastructureClients.get_memgraph()
        try:
            async with memgraph.session() as mg_session:
                cypher = "MATCH (source)-[r:RELATED_TO]->() "
                if tenant_id:
                    cypher += "WHERE coalesce(r.tenant_id, source.tenant_id) = $tenant_id "
                cypher += "RETURN DISTINCT r.doc_id AS doc_id, coalesce(r.tenant_id, source.tenant_id) AS tenant_id, count(r) AS cnt;"
                
                params = {"tenant_id": tenant_id} if tenant_id else {}
                mg_res = await mg_session.run(cypher, **params)
                records = await mg_res.data()
                
                total_edges = 0
                for rec in records:
                    d_id = rec.get("doc_id")
                    t_id = rec.get("tenant_id") or "unknown"
                    cnt = rec.get("cnt", 0)
                    total_edges += cnt
                    if d_id and (
                        d_id not in valid_pg_doc_ids
                        or doc_tenant_map.get(d_id) != t_id
                    ):
                        report.drifts.append(
                            StorageDriftItem(
                                storage="memgraph",
                                drift_type="orphan_graph_edge",
                                doc_id=d_id,
                                tenant_id=t_id,
                                details={"edge_count": cnt}
                            )
                        )
                report.memgraph_edges_count = total_edges

        except Exception as e:
            logger.error(f"Failed to audit Memgraph graph relations: {e}")

        # 4. Audit MinIO Storage Objects
        minio_client = InfrastructureClients.get_minio()
        try:
            def scan_minio_objects():
                buckets = {settings.MINIO_BUCKET_NAME}
                buckets.update(d.minio_bucket for d in docs if d.minio_bucket)
                return [
                    (bucket, obj)
                    for bucket in buckets
                    for obj in minio_client.list_objects(bucket, recursive=True)
                ]

            minio_objs = await asyncio.to_thread(scan_minio_objects)
            report.minio_objects_count = len(minio_objs)

            existing_object_keys = {
                f"{bucket}:{getattr(obj, 'object_name', '')}"
                for bucket, obj in minio_objs
            }
            for bucket, obj in minio_objs:
                obj_name = obj.object_name or ""
                # Parse tenant_id and doc_id from object path
                parts = obj_name.split("/")
                # Pattern: tenant_id/doc_id/... or uploads/tenant_id/...
                extracted_doc_id = None
                extracted_tenant = "default"
                for p in parts:
                    if len(p) == 36 and "-" in p: # UUID format
                        extracted_doc_id = p
                    elif p.startswith("tenant_"):
                        extracted_tenant = p

                # Also detect legacy/non-UUID object names.  The exact object
                # key stored in PostgreSQL is authoritative.
                if f"{bucket}:{obj_name}" not in valid_object_keys:
                    report.drifts.append(
                        StorageDriftItem(
                            storage="minio",
                            drift_type="orphan_file",
                            doc_id=extracted_doc_id or "unknown",
                            tenant_id=extracted_tenant,
                            details={"bucket": bucket, "object_name": obj_name, "size": obj.size},
                        )
                    )

            for document in docs:
                object_key = f"{document.minio_bucket}:{document.minio_key}"
                if object_key not in existing_object_keys:
                    report.drifts.append(
                        StorageDriftItem(
                            storage="minio",
                            drift_type="missing_file",
                            doc_id=document.id,
                            tenant_id=document.tenant_id,
                            details={
                                "bucket": document.minio_bucket,
                                "object_name": document.minio_key,
                            },
                        )
                    )
        except Exception as e:
            logger.error(f"Failed to audit MinIO objects: {e}")

        # Record drift telemetry in Prometheus
        for d in report.drifts:
            prometheus_metrics.record_reconciliation_drift(d.storage, d.drift_type)

        return report

    @classmethod
    async def repair_consistency(
        cls,
        report: ReconciliationReport,
        dry_run: bool = True
    ) -> ReconciliationResult:
        """
        Safely repairs consistency drifts according to the provided report.
        In dry_run mode, no mutations occur.
        """
        import time
        start_t = time.perf_counter()
        result = ReconciliationResult(dry_run=dry_run)

        if dry_run or not report.has_drift:
            result.duration_seconds = round(time.perf_counter() - start_t, 3)
            return result

        qdrant = InfrastructureClients.get_qdrant()
        memgraph = InfrastructureClients.get_memgraph()
        minio_client = InfrastructureClients.get_minio()

        for drift in report.drifts:
            try:
                # 1. Clean orphan vector in Qdrant
                if drift.storage == "qdrant" and drift.drift_type == "orphan_vector":
                    await qdrant.delete(
                        collection_name=settings.QDRANT_COLLECTION,
                        points_selector=qmodels.FilterSelector(
                            filter=qmodels.Filter(
                                must=[
                                    qmodels.FieldCondition(
                                        key="doc_id",
                                        match=qmodels.MatchValue(value=drift.doc_id),
                                    ),
                                    qmodels.FieldCondition(
                                        key="tenant_id",
                                        match=qmodels.MatchValue(value=drift.tenant_id),
                                    ),
                                ]
                            )
                        )
                    )
                    result.cleaned_orphan_vectors += 1

                # 2. Clean orphan graph edge in Memgraph
                elif drift.storage == "memgraph" and drift.drift_type == "orphan_graph_edge":
                    async with memgraph.session() as mg_session:
                        await mg_session.run(
                            "MATCH (source)-[r:RELATED_TO {doc_id: $doc_id}]->() "
                            "WHERE coalesce(r.tenant_id, source.tenant_id) = $tenant_id "
                            "DELETE r;",
                            doc_id=drift.doc_id,
                            tenant_id=drift.tenant_id,
                        )
                    result.cleaned_orphan_edges += 1

                # 3. Clean orphan object in MinIO
                elif drift.storage == "minio" and drift.drift_type == "orphan_file":
                    obj_name = drift.details.get("object_name")
                    if obj_name:
                        await asyncio.to_thread(
                            minio_client.remove_object,
                            drift.details.get("bucket", settings.MINIO_BUCKET_NAME),
                            obj_name
                        )
                        result.cleaned_orphan_files += 1

            except Exception as err:
                logger.error(f"Failed to repair drift ({drift.storage} - {drift.drift_type}): {err}")

        result.duration_seconds = round(time.perf_counter() - start_t, 3)
        return result
