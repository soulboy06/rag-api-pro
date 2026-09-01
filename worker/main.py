"""
Independent Worker Daemon Process
Listens on Redis Streams consumer group, claims tasks, updates PostgreSQL FSM,
coordinates document processing with active HeartbeatLeaseGuardian, and runs periodic OrphanTaskRecoveryScanner.
"""
import os
import sys
import asyncio
import signal
import socket
import json
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

# Ensure utf-8 stdout on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.config import settings
from src.core.clients import InfrastructureClients
from src.core.database import init_db
from src.services.task_service import TaskService
from src.models.db_models import Document
from src.core.tenant.config_manager import TenantConfigManager
from src.core.tenant.prompts import TenantPromptManager
from src.core.tasks.outbox import TaskOutboxService
from src.core.tasks.lease import HeartbeatLeaseGuardian
from src.core.tasks.recovery import OrphanTaskRecoveryScanner
from worker.processor import DocumentProcessor

WORKER_ID = f"worker-{socket.gethostname()}-{os.getpid()}"


async def run_worker():
    print(f"[WORKER {WORKER_ID}] Starting RAG Worker Daemon...", flush=True)

    # Ensure DB & Clients are ready
    await init_db()
    await InfrastructureClients.init_infrastructure()
    redis_client = InfrastructureClients.get_redis()

    engine = create_async_engine(settings.DATABASE_URL, pool_size=5)
    SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    stream_name = settings.REDIS_STREAM_NAME
    group_name = settings.REDIS_CONSUMER_GROUP

    print(f"[WORKER {WORKER_ID}] Listening on Stream '{stream_name}', Group '{group_name}'", flush=True)

    running = True

    def handle_stop(signum, frame):
        nonlocal running
        print(f"\n[WORKER {WORKER_ID}] Stop signal received. Shutting down gracefully...", flush=True)
        running = False

    try:
        signal.signal(signal.SIGINT, handle_stop)
        signal.signal(signal.SIGTERM, handle_stop)
    except Exception:
        pass

    # 0. Start periodic background orphan task recovery scanner
    async def recovery_loop():
        while running:
            try:
                await asyncio.sleep(15.0)
                if not running:
                    break
                rec_res = await OrphanTaskRecoveryScanner.scan_and_recover(max_attempts=3)
                if rec_res.get("recovered_count", 0) > 0 or rec_res.get("dead_letter_count", 0) > 0:
                    print(f"[WORKER {WORKER_ID} RECOVERY] Sweep completed: {rec_res}", flush=True)
            except asyncio.CancelledError:
                break
            except Exception as scan_err:
                print(f"[WORKER {WORKER_ID} RECOVERY] Error during sweep: {scan_err}", flush=True)

    scanner_task = asyncio.create_task(recovery_loop())

    while running:
        try:
            # The outbox is the durable source of truth for delivery.  Publishing
            # it here also lets a worker heal events after an API restart.
            try:
                await TaskOutboxService.publish_pending(limit=10)
            except Exception as outbox_err:
                print(f"[WORKER {WORKER_ID} OUTBOX] Publish sweep failed: {outbox_err}", flush=True)

            # 1. First claim any orphaned messages from dead workers idle for > 2s
            claimed_messages = []
            try:
                autoclaim_res = await redis_client.xautoclaim(
                    name=stream_name,
                    groupname=group_name,
                    consumername=WORKER_ID,
                    min_idle_time=2000,
                    start_id="0-0",
                    count=1
                )
                if autoclaim_res and len(autoclaim_res) > 1 and autoclaim_res[1]:
                    claimed_messages = [(stream_name, autoclaim_res[1])]
            except Exception:
                pass

            if claimed_messages:
                messages = claimed_messages
            else:
                # 2. Read new messages from Stream
                messages = await redis_client.xreadgroup(
                    groupname=group_name,
                    consumername=WORKER_ID,
                    streams={stream_name: ">"},
                    count=1,
                    block=1500,
                )

            if not messages:
                await asyncio.sleep(0.5)
                continue

            for stream, msg_list in messages:
                for msg_id, payload in msg_list:
                    # Normalize bytes to strings
                    norm_payload = {
                        k.decode("utf-8") if isinstance(k, bytes) else k:
                        v.decode("utf-8") if isinstance(v, bytes) else v
                        for k, v in payload.items()
                    }

                    task_id = norm_payload.get("task_id")
                    tenant_id = norm_payload.get("tenant_id")
                    kb_id = norm_payload.get("kb_id")
                    doc_id = norm_payload.get("doc_id")
                    minio_bucket = norm_payload.get("minio_bucket")
                    minio_key = norm_payload.get("minio_key")
                    filename = norm_payload.get("filename")
                    force_parser = norm_payload.get("force_parser")
                    options = {}
                    options_json = norm_payload.get("options_json")
                    if options_json:
                        try:
                            options = json.loads(options_json)
                        except (TypeError, json.JSONDecodeError):
                            print(
                                f"[WORKER {WORKER_ID}] Invalid task options for {task_id}; using defaults",
                                flush=True,
                            )
                    force_parser = force_parser or options.get("force_parser")

                    if not task_id:
                        await redis_client.xack(stream_name, group_name, msg_id)
                        continue

                    print(f"[WORKER {WORKER_ID}] Claimed Task {task_id} for doc '{filename}' (doc_id={doc_id})", flush=True)

                    ack_message = False
                    async with SessionLocal() as db:
                        # 1. Acquire task lease (CAS transition to RUNNING + fencing token bump)
                        acquired, task = await TaskService.acquire_task_lease(
                            db=db,
                            task_id=task_id,
                            worker_id=WORKER_ID,
                            lease_seconds=60.0
                        )
                        if not acquired or not task:
                            print(f"[WORKER {WORKER_ID}] Task {task_id} not in claimable state or already claimed. Skipping.", flush=True)
                            await redis_client.xack(stream_name, group_name, msg_id)
                            continue

                        # Treat the stream payload as a delivery hint, not as
                        # the authority for ownership or file location. A
                        # stale/forged payload must not make a worker process a
                        # different tenant's object under the current task ID.
                        canonical_doc = (
                            await db.execute(
                                select(Document).where(
                                    Document.id == task.doc_id,
                                    Document.tenant_id == task.tenant_id,
                                    Document.kb_id == task.kb_id,
                                )
                            )
                        ).scalar_one_or_none()
                        if not canonical_doc:
                            await TaskService.update_task_status_cas(
                                db=db,
                                task_id=task_id,
                                from_status="RUNNING",
                                to_status="FAILED",
                                expected_fencing_token=task.fencing_token,
                                worker_id=WORKER_ID,
                                error_msg="Task references a missing or cross-tenant document",
                            )
                            await redis_client.xack(stream_name, group_name, msg_id)
                            continue

                        tenant_id = task.tenant_id
                        kb_id = task.kb_id
                        doc_id = task.doc_id
                        minio_bucket = canonical_doc.minio_bucket
                        minio_key = canonical_doc.minio_key
                        filename = canonical_doc.filename

                        # Pin the durable tenant configuration and prompt
                        # snapshot for this ingestion attempt. Parser/model
                        # options are taken from PostgreSQL, not from stale
                        # delivery payload fields.
                        tenant_config = await TenantConfigManager.load_latest_config_db(
                            db, tenant_id
                        )
                        prompt_snapshot = await TenantPromptManager.load_snapshot_db(
                            db, tenant_id, kb_id
                        )
                        options = {
                            **options,
                            "embedding_model": tenant_config.embedding_model,
                            "llm_model": tenant_config.llm_model,
                            "custom_entity_types": list(prompt_snapshot.custom_entities),
                        }

                        current_fencing_token = task.fencing_token
                        target_generation = task.target_generation or int(
                            options.get("target_generation", 1)
                        )

                        # Progress callback to update Task stage and percentage
                        async def update_progress(stage: str, percent: int):
                            async with SessionLocal() as progress_db:
                                updated = await TaskService.update_task_progress_cas(
                                    db=progress_db,
                                    task_id=task_id,
                                    worker_id=WORKER_ID,
                                    expected_fencing_token=current_fencing_token,
                                    stage=stage,
                                    progress_percent=percent,
                                )
                                if not updated:
                                    raise RuntimeError(
                                        f"Task ownership lost while reporting progress for {task_id}"
                                    )

                        # 2. Process Document under active HeartbeatLeaseGuardian
                        try:
                            async with HeartbeatLeaseGuardian(
                                task_id=task_id,
                                worker_id=WORKER_ID,
                                fencing_token=current_fencing_token,
                                heartbeat_interval_seconds=10.0,
                                lease_duration_seconds=30.0
                            ):
                                result = await DocumentProcessor.process_document(
                                    tenant_id=tenant_id,
                                    kb_id=kb_id,
                                    doc_id=doc_id,
                                    minio_bucket=minio_bucket,
                                    minio_key=minio_key,
                                    filename=filename,
                                    progress_callback=update_progress,
                                    force_parser=force_parser,
                                    options=options,
                                    generation=target_generation,
                                )

                            # 3. Publish the newly built generation and mark
                            # the task successful in one PostgreSQL commit.
                            partial_result = result.get("status") == "PARTIAL_SUCCEEDED"
                            transitioned = await TaskService.complete_task_with_generation(
                                db=db,
                                task_id=task_id,
                                tenant_id=tenant_id,
                                doc_id=doc_id,
                                expected_fencing_token=current_fencing_token,
                                worker_id=WORKER_ID,
                                generation=target_generation,
                                final_status="PARTIAL_SUCCEEDED" if partial_result else "SUCCEEDED",
                                publish_generation=not partial_result,
                            )
                            if not transitioned:
                                raise RuntimeError(
                                    f"Task ownership lost before success commit for {task_id}"
                                )
                            ack_message = True
                            print(f"[WORKER {WORKER_ID}] Task {task_id} {result['status']}. Indexed {result['chunks_count']} chunks, {result['triplets_count']} graph relations.", flush=True)
                        except Exception as err:
                            error_text = str(err)
                            print(f"[WORKER {WORKER_ID}] Task {task_id} FAILED: {error_text}", flush=True)
                            failed = await TaskService.update_task_status_cas(
                                db=db,
                                task_id=task_id,
                                from_status="RUNNING",
                                to_status="FAILED",
                                expected_fencing_token=current_fencing_token,
                                worker_id=WORKER_ID,
                                error_msg=error_text[:1000]
                            )
                            # If CAS fails, a newer worker owns the task.  Keep
                            # the stream message pending so that ownership is
                            # resolved by the normal recovery/claim path.
                            ack_message = failed

                    # 4. ACK Message in Stream
                    if ack_message:
                        await redis_client.xack(stream_name, group_name, msg_id)

        except asyncio.CancelledError:
            break
        except Exception as e:
            if running:
                print(f"[WORKER {WORKER_ID}] Error in worker loop: {e}", flush=True)
                await asyncio.sleep(1.0)

    scanner_task.cancel()
    try:
        await scanner_task
    except asyncio.CancelledError:
        pass

    print(f"[WORKER {WORKER_ID}] Worker exited cleanly.", flush=True)
    await InfrastructureClients.close_all()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_worker())
