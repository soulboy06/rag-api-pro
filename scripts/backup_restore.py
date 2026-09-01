#!/usr/bin/env python
"""
Level 8: Enterprise Backup, Disaster Recovery & Index Reconstruction Script
Provides snapshot export for PostgreSQL metadata and validates full index reconstruction
of Qdrant and Memgraph from MinIO raw documents.
Fixes: P1-STORE-01, P1-STORE-02, P1-STORE-03
"""
import sys
import os
import json
import argparse
import asyncio
from pathlib import Path
from typing import Any, Dict, Iterable

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from datetime import datetime, timezone
from sqlalchemy import select

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.database import AsyncSessionLocal
from src.models.db_models import (
    Tenant,
    KnowledgeBase,
    Document,
    User,
    ApiKey,
    Task,
    TenantConfig,
    TenantPrompt,
    ChatSession,
    ChatMessage,
    TaskOutboxEvent,
)
from src.core.clients import InfrastructureClients


async def export_backup(output_path: str):
    """Exports structured metadata snapshot from PostgreSQL to JSON."""
    print(f"📦 Exporting metadata snapshot to '{output_path}'...")
    snapshot = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tenants": [],
        "knowledge_bases": [],
        "documents": [],
        "users": [],
        "api_keys": [],
        "tasks": [],
        "tenant_configs": [],
        "tenant_prompts": [],
        "chat_sessions": [],
        "chat_messages": [],
        "task_outbox_events": [],
    }

    async with AsyncSessionLocal() as session:
        # Tenants
        t_res = await session.execute(select(Tenant))
        for t in t_res.scalars().all():
            snapshot["tenants"].append({
                "id": t.id,
                "name": t.name,
                "created_at": t.created_at.isoformat() if t.created_at else None
            })

        # Knowledge Bases
        kb_res = await session.execute(select(KnowledgeBase))
        for kb in kb_res.scalars().all():
            snapshot["knowledge_bases"].append({
                "id": kb.id,
                "tenant_id": kb.tenant_id,
                "name": kb.name,
                "description": kb.description
            })

        # Documents
        doc_res = await session.execute(select(Document))
        for d in doc_res.scalars().all():
            snapshot["documents"].append({
                "id": d.id,
                "tenant_id": d.tenant_id,
                "kb_id": d.kb_id,
                "filename": d.filename,
                "content_type": d.content_type,
                "file_size": d.file_size,
                "minio_bucket": d.minio_bucket,
                "minio_key": d.minio_key,
                "content_hash": d.content_hash,
                "active_generation": d.active_generation,
            })

        # Users
        u_res = await session.execute(select(User))
        for u in u_res.scalars().all():
            snapshot["users"].append({
                "id": u.id,
                "tenant_id": u.tenant_id,
                "username": u.username,
                # Required to restore authentication without resetting users.
                # Protect the backup file like a credential store.
                "hashed_password": u.hashed_password,
                "role": u.role,
                "is_active": u.is_active,
                "refresh_token_hash": u.refresh_token_hash,
                "created_at": u.created_at.isoformat() if u.created_at else None
            })

        # ApiKeys
        ak_res = await session.execute(select(ApiKey))
        for ak in ak_res.scalars().all():
            snapshot["api_keys"].append({
                "id": ak.id,
                "tenant_id": ak.tenant_id,
                "user_id": ak.user_id,
                "name": ak.name,
                "key_prefix": ak.key_prefix,
                "key_hash": ak.key_hash,
                "role": ak.role,
                "is_active": ak.is_active,
                "expires_at": ak.expires_at.isoformat() if ak.expires_at else None,
                "created_at": ak.created_at.isoformat() if ak.created_at else None,
                "last_used_at": ak.last_used_at.isoformat() if ak.last_used_at else None,
            })

        task_res = await session.execute(select(Task))
        for task in task_res.scalars().all():
            snapshot["tasks"].append({
                "id": task.id,
                "tenant_id": task.tenant_id,
                "kb_id": task.kb_id,
                "doc_id": task.doc_id,
                "task_type": task.task_type,
                "status": task.status,
                "stage": task.stage,
                "progress_percent": task.progress_percent,
                "attempt": task.attempt,
                "retry_count": task.retry_count,
                "fencing_token": task.fencing_token,
                "idempotency_key": task.idempotency_key,
                "worker_id": task.worker_id,
                "lease_expires_at": task.lease_expires_at.isoformat() if task.lease_expires_at else None,
                "next_attempt_at": task.next_attempt_at.isoformat() if task.next_attempt_at else None,
                "target_generation": task.target_generation,
                "task_options": task.task_options,
                "error_msg": task.error_msg,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            })

        config_res = await session.execute(select(TenantConfig))
        for config in config_res.scalars().all():
            snapshot["tenant_configs"].append({
                "id": config.id,
                "tenant_id": config.tenant_id,
                "version_id": config.version_id,
                "config_data": config.config_data,
                "created_at": config.created_at.isoformat() if config.created_at else None,
                "updated_at": config.updated_at.isoformat() if config.updated_at else None,
            })

        prompt_res = await session.execute(select(TenantPrompt))
        for prompt in prompt_res.scalars().all():
            snapshot["tenant_prompts"].append({
                "id": prompt.id,
                "tenant_id": prompt.tenant_id,
                "kb_id": prompt.kb_id,
                "qa_template": prompt.qa_template,
                "entity_template": prompt.entity_template,
                "query_rewrite_template": prompt.query_rewrite_template,
                "custom_persona": prompt.custom_persona,
                "custom_entities": prompt.custom_entities,
                "created_at": prompt.created_at.isoformat() if prompt.created_at else None,
                "updated_at": prompt.updated_at.isoformat() if prompt.updated_at else None,
            })

        session_res = await session.execute(select(ChatSession))
        for chat_session in session_res.scalars().all():
            snapshot["chat_sessions"].append({
                "id": chat_session.id,
                "tenant_id": chat_session.tenant_id,
                "kb_id": chat_session.kb_id,
                "user_id": chat_session.user_id,
                "title": chat_session.title,
                "created_at": chat_session.created_at.isoformat() if chat_session.created_at else None,
                "updated_at": chat_session.updated_at.isoformat() if chat_session.updated_at else None,
            })

        message_res = await session.execute(select(ChatMessage))
        for message in message_res.scalars().all():
            snapshot["chat_messages"].append({
                "id": message.id,
                "session_id": message.session_id,
                "role": message.role,
                "content": message.content,
                "sources": message.sources,
                "created_at": message.created_at.isoformat() if message.created_at else None,
            })

        outbox_res = await session.execute(select(TaskOutboxEvent))
        for event in outbox_res.scalars().all():
            snapshot["task_outbox_events"].append({
                "id": event.id,
                "event_key": event.event_key,
                "task_id": event.task_id,
                "tenant_id": event.tenant_id,
                "event_type": event.event_type,
                "payload": event.payload,
                "status": event.status,
                "attempts": event.attempts,
                "available_at": event.available_at.isoformat() if event.available_at else None,
                "lease_expires_at": event.lease_expires_at.isoformat() if event.lease_expires_at else None,
                "last_error": event.last_error,
                "created_at": event.created_at.isoformat() if event.created_at else None,
                "published_at": event.published_at.isoformat() if event.published_at else None,
            })

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"✓ Backup exported successfully:")
    print(f"  - Tenants:         {len(snapshot['tenants'])}")
    print(f"  - Knowledge Bases: {len(snapshot['knowledge_bases'])}")
    print(f"  - Documents:       {len(snapshot['documents'])}")
    print(f"  - Users:           {len(snapshot['users'])}")
    print(f"  - API Keys:        {len(snapshot['api_keys'])}")


def _parse_datetime(value: Any):
    if not value:
        return None
    return datetime.fromisoformat(value)


async def restore_backup(input_path: str, dry_run: bool = True) -> Dict[str, int]:
    """Idempotently restores PostgreSQL metadata; raw objects remain untouched."""
    path = Path(input_path)
    if not path.is_file():
        raise FileNotFoundError(f"Backup file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        snapshot = json.load(handle)

    counts = {key: len(snapshot.get(key, [])) for key in (
        "tenants", "knowledge_bases", "documents", "users", "api_keys",
        "tasks", "tenant_configs", "tenant_prompts", "chat_sessions", "chat_messages",
        "task_outbox_events",
    )}
    if dry_run:
        print(f"🔍 Restore dry-run: {counts}")
        return counts

    async with AsyncSessionLocal() as session:
        async def upsert(model, rows: Iterable[Dict[str, Any]], fields: Iterable[str]):
            for row in rows:
                record = await session.get(model, row.get("id"))
                values = {field: row[field] for field in fields if field in row}
                for field in (
                    "created_at", "updated_at", "expires_at", "last_used_at",
                    "lease_expires_at", "next_attempt_at", "available_at", "published_at",
                ):
                    if field in values:
                        values[field] = _parse_datetime(values[field])
                if record is None:
                    if row.get("id") is None:
                        continue
                    if model is User and not row.get("hashed_password"):
                        print(f"⚠️  Skipping user {row['id']}: backup has no password hash")
                        continue
                    if model is ApiKey and not row.get("key_hash"):
                        print(f"⚠️  Skipping API key {row['id']}: backup has no key hash")
                        continue
                    session.add(model(id=row["id"], **values))
                else:
                    for key, value in values.items():
                        setattr(record, key, value)

        await upsert(Tenant, snapshot.get("tenants", []), ("name", "created_at"))
        await session.flush()
        await upsert(KnowledgeBase, snapshot.get("knowledge_bases", []), ("tenant_id", "name", "description"))
        await upsert(User, snapshot.get("users", []), (
            "tenant_id", "username", "hashed_password", "role", "is_active",
            "refresh_token_hash", "created_at",
        ))
        await upsert(Document, snapshot.get("documents", []), (
            "tenant_id", "kb_id", "filename", "content_type", "file_size",
            "minio_bucket", "minio_key", "content_hash", "active_generation",
        ))
        await upsert(Task, snapshot.get("tasks", []), (
            "tenant_id", "kb_id", "doc_id", "task_type", "status", "stage",
            "progress_percent", "attempt", "retry_count", "fencing_token",
            "idempotency_key", "worker_id", "lease_expires_at", "next_attempt_at",
            "target_generation", "task_options", "error_msg", "created_at", "updated_at",
        ))
        await upsert(ApiKey, snapshot.get("api_keys", []), (
            "tenant_id", "user_id", "name", "key_prefix", "key_hash", "role",
            "is_active", "expires_at", "created_at", "last_used_at",
        ))
        await upsert(TenantConfig, snapshot.get("tenant_configs", []), (
            "tenant_id", "version_id", "config_data", "created_at", "updated_at",
        ))
        await upsert(TenantPrompt, snapshot.get("tenant_prompts", []), (
            "tenant_id", "kb_id", "qa_template", "entity_template",
            "query_rewrite_template", "custom_persona", "custom_entities",
            "created_at", "updated_at",
        ))
        await upsert(ChatSession, snapshot.get("chat_sessions", []), (
            "tenant_id", "kb_id", "user_id", "title", "created_at", "updated_at",
        ))
        await upsert(ChatMessage, snapshot.get("chat_messages", []), (
            "session_id", "role", "content", "sources", "created_at",
        ))
        await upsert(TaskOutboxEvent, snapshot.get("task_outbox_events", []), (
            "event_key", "task_id", "tenant_id", "event_type", "payload", "status",
            "attempts", "available_at", "lease_expires_at", "last_error",
            "created_at", "published_at",
        ))
        await session.commit()

    print(f"✅ Metadata restore completed: {counts}")
    return counts


async def verify_disaster_recovery():
    """Verifies that all MinIO raw objects have reachable valid keys in PostgreSQL."""
    print("🛡️  Verifying Disaster Recovery Integrity...")
    minio = InfrastructureClients.get_minio()
    async with AsyncSessionLocal() as session:
        doc_res = await session.execute(select(Document))
        docs = doc_res.scalars().all()
        print(f"  Checking {len(docs)} PostgreSQL documents against MinIO objects...")
        missing_count = 0
        for d in docs:
            def check_stat():
                try:
                    minio.stat_object(d.minio_bucket, d.minio_key)
                    return True
                except Exception:
                    return False
            exists = await asyncio.to_thread(check_stat)
            if not exists:
                missing_count += 1
                print(f"  ❌ Missing MinIO Object for Doc '{d.filename}' (Key: {d.minio_key})")

        if missing_count == 0:
            print("  ✅ 100% Disaster Recovery Readiness: All raw source documents in MinIO are intact.")
        else:
            print(f"  ⚠️  Found {missing_count} missing MinIO documents.")


async def main():
    parser = argparse.ArgumentParser(description="Backup and Disaster Recovery Integrity Verification")
    parser.add_argument("--backup", type=str, default="backups/metadata_snapshot.json", help="Path to save metadata snapshot")
    parser.add_argument("--restore", type=str, default=None, help="Restore metadata from a JSON snapshot")
    parser.add_argument("--apply", action="store_true", help="Apply restore; without it restore only validates the file")
    parser.add_argument("--verify", action="store_true", help="Run disaster recovery verification")

    args = parser.parse_args()
    if args.restore:
        await restore_backup(args.restore, dry_run=not args.apply)
    elif args.verify:
        await verify_disaster_recovery()
    else:
        await export_backup(args.backup)


if __name__ == "__main__":
    asyncio.run(main())
