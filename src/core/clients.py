"""
Level 1: Infrastructure Clients & Unified Lifecycle Management
Manages connections and initialization for Redis, MinIO, Qdrant, and Memgraph.
Guarantees event-loop aware client instantiation for async safety across tests and workers.
Fixes: P1-REMOTE-01, P1-TASK-05
"""
import asyncio
from typing import Optional, Dict, Any, List
import redis.asyncio as aioredis
from minio import Minio
from minio.error import S3Error
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from neo4j import AsyncGraphDatabase, AsyncDriver
import httpx

from src.core.config import settings
from src.core.exceptions import StorageUnavailableError, ConfigurationError


class InfrastructureClients:
    _redis: Optional[aioredis.Redis] = None
    _redis_loop: Optional[asyncio.AbstractEventLoop] = None

    _minio: Optional[Minio] = None

    _qdrant: Optional[AsyncQdrantClient] = None
    _qdrant_loop: Optional[asyncio.AbstractEventLoop] = None

    _memgraph: Optional[AsyncDriver] = None
    _memgraph_loop: Optional[asyncio.AbstractEventLoop] = None

    _http_client: Optional[httpx.AsyncClient] = None
    _http_loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    def get_redis(cls) -> aioredis.Redis:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if cls._redis is None or (current_loop and cls._redis_loop != current_loop):
            cls._redis = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                encoding="utf-8"
            )
            cls._redis_loop = current_loop
        return cls._redis

    @classmethod
    def get_minio(cls) -> Minio:
        if cls._minio is None:
            cls._minio = Minio(
                endpoint=settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY,
                secret_key=settings.MINIO_SECRET_KEY,
                secure=settings.MINIO_SECURE,
            )
        return cls._minio

    @classmethod
    def get_qdrant(cls) -> AsyncQdrantClient:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if cls._qdrant is None or (current_loop and cls._qdrant_loop != current_loop):
            cls._qdrant = AsyncQdrantClient(
                host=settings.QDRANT_HOST,
                port=settings.QDRANT_PORT,
            )
            cls._qdrant_loop = current_loop
        return cls._qdrant

    @classmethod
    def get_memgraph(cls) -> AsyncDriver:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if cls._memgraph is None or (current_loop and cls._memgraph_loop != current_loop):
            uri = f"bolt://{settings.MEMGRAPH_HOST}:{settings.MEMGRAPH_PORT}"
            auth = (settings.MEMGRAPH_USER or "", settings.MEMGRAPH_PASSWORD or "") if settings.MEMGRAPH_USER else None
            cls._memgraph = AsyncGraphDatabase.driver(uri, auth=auth)
            cls._memgraph_loop = current_loop
        return cls._memgraph

    @classmethod
    async def get_http_client(cls) -> httpx.AsyncClient:
        """Returns one pooled async HTTP client for the current event loop.

        Remote model/parser calls share connection pools and keep-alive
        sockets. A loop change (common in tests and process reloads) closes the
        old client before creating a new one so sockets are not leaked across
        lifecycles.
        """
        current_loop = asyncio.get_running_loop()
        if cls._http_client is None or cls._http_loop != current_loop:
            old_client = cls._http_client
            cls._http_client = None
            cls._http_loop = None
            if old_client is not None:
                try:
                    await old_client.aclose()
                except Exception:
                    pass
            cls._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0),
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                ),
            )
            cls._http_loop = current_loop
        return cls._http_client

    @classmethod
    def get_presigned_download_url(
        cls,
        object_name: str,
        bucket_name: Optional[str] = None,
        expires_seconds: int = 3600
    ) -> str:
        """Generates a secure temporary presigned download URL."""
        from datetime import timedelta
        minio_client = cls.get_minio()
        bucket = bucket_name or settings.MINIO_BUCKET_NAME
        url = minio_client.presigned_get_object(
            bucket_name=bucket,
            object_name=object_name,
            expires=timedelta(seconds=expires_seconds),
        )
        return url

    @classmethod
    def delete_minio_object(cls, object_name: str, bucket_name: Optional[str] = None) -> bool:
        """Safely removes an object from MinIO."""
        minio_client = cls.get_minio()
        bucket = bucket_name or settings.MINIO_BUCKET_NAME
        try:
            minio_client.remove_object(bucket_name=bucket, object_name=object_name)
            return True
        except S3Error as exc:
            # DELETE is intentionally idempotent: a retry after a successful
            # first delete must not keep PostgreSQL metadata stuck forever.
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return True
            return False
        except Exception:
            return False

    @classmethod
    async def init_infrastructure(cls) -> None:
        """Initializes buckets, collections, consumer groups and indexes."""
        # 1. MinIO Bucket
        minio_client = cls.get_minio()
        if not minio_client.bucket_exists(settings.MINIO_BUCKET_NAME):
            minio_client.make_bucket(settings.MINIO_BUCKET_NAME)

        # 2. Redis Streams & Consumer Group
        redis_client = cls.get_redis()
        try:
            await redis_client.xgroup_create(
                name=settings.REDIS_STREAM_NAME,
                groupname=settings.REDIS_CONSUMER_GROUP,
                id="0",
                mkstream=True
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise StorageUnavailableError(
                    f"Redis stream initialization failed: {e}"
                ) from e

        # 3. Qdrant Collection
        qdrant_client = cls.get_qdrant()
        collections = await qdrant_client.get_collections()
        collection_names = [c.name for c in collections.collections]
        
        need_create = settings.QDRANT_COLLECTION not in collection_names
        if not need_create:
            info = await qdrant_client.get_collection(settings.QDRANT_COLLECTION)
            existing_size = info.config.params.vectors.size if hasattr(info.config.params.vectors, 'size') else None
            if existing_size and existing_size != settings.VECTOR_DIMENSION:
                raise ConfigurationError(
                    f"Qdrant collection '{settings.QDRANT_COLLECTION}' has dimension "
                    f"{existing_size}, expected {settings.VECTOR_DIMENSION}; refusing destructive recreation"
                )

        if need_create:
            await qdrant_client.create_collection(
                collection_name=settings.QDRANT_COLLECTION,
                vectors_config=qmodels.VectorParams(
                    size=settings.VECTOR_DIMENSION,
                    distance=qmodels.Distance.COSINE
                )
            )
        # Ensure indexes exist for both new and previously created collections.
        for field_name in ("tenant_id", "kb_id", "doc_id"):
            await qdrant_client.create_payload_index(
                collection_name=settings.QDRANT_COLLECTION,
                field_name=field_name,
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
            )
        for field_name in ("content", "search_index_text"):
            await qdrant_client.create_payload_index(
                collection_name=settings.QDRANT_COLLECTION,
                field_name=field_name,
                field_schema=qmodels.PayloadSchemaType.TEXT,
            )

        # 4. Memgraph Constraint/Index
        memgraph = cls.get_memgraph()
        async with memgraph.session() as session:
            try:
                await session.run("CREATE INDEX ON :Entity(name);")
            except Exception:
                pass
            try:
                await session.run("CREATE INDEX ON :Entity(tenant_id);")
            except Exception:
                pass

    @classmethod
    async def close_all(cls) -> None:
        """Gracefully closes all external client connections."""
        if cls._redis:
            try:
                await cls._redis.close()
            except Exception:
                pass
            cls._redis = None
            cls._redis_loop = None
        if cls._qdrant:
            try:
                await cls._qdrant.close()
            except Exception:
                pass
            cls._qdrant = None
            cls._qdrant_loop = None
        if cls._memgraph:
            try:
                await cls._memgraph.close()
            except Exception:
                pass
            cls._memgraph = None
            cls._memgraph_loop = None
        if cls._http_client:
            try:
                await cls._http_client.aclose()
            except Exception:
                pass
            cls._http_client = None
            cls._http_loop = None
