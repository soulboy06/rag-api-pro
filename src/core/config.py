"""
Level 0: Modular Strictly-Typed Configuration Center
Loads, validates, and freezes sub-settings for Security, Storage, Models, and Observability.
Prevents unvalidated configurations from booting the service.
Fixes: P0-CORE-01, P1-CORE-13, P1-CORE-14, P3-CODE-05
"""
from typing import Optional, List, Dict, Any
from pathlib import Path
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecurityConfig(BaseModel):
    SECRET_KEY: str = Field(
        default="rag-pro-super-secret-key-change-in-production-2026",
        min_length=16,
        description="JWT Signing secret key"
    )
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=1440, ge=5, le=43200)
    PASSWORD_MIN_LENGTH: int = Field(default=6, ge=6, le=64)


class DatabaseConfig(BaseModel):
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://rag_user:rag_password@127.0.0.1:5432/rag_pro",
        description="Async PostgreSQL connection URI"
    )
    SYNC_DATABASE_URL: str = Field(
        default="postgresql://rag_user:rag_password@127.0.0.1:5432/rag_pro",
        description="Sync PostgreSQL connection URI for migrations"
    )
    POOL_SIZE: int = Field(default=20, ge=5, le=100)
    MAX_OVERFLOW: int = Field(default=10, ge=0, le=50)
    POOL_RECYCLE_SECONDS: int = Field(default=3600, ge=60)


class RedisConfig(BaseModel):
    REDIS_URL: str = Field(default="redis://127.0.0.1:6379/0")
    REDIS_STREAM_NAME: str = Field(default="stream:rag_tasks")
    REDIS_CONSUMER_GROUP: str = Field(default="cg:rag_workers")
    TASK_LEASE_TIMEOUT_SECONDS: int = Field(default=300, ge=30, le=3600)


class MinioConfig(BaseModel):
    MINIO_ENDPOINT: str = Field(default="127.0.0.1:9000")
    MINIO_ACCESS_KEY: str = Field(default="minioadmin")
    MINIO_SECRET_KEY: str = Field(default="minioadmin_secret")
    MINIO_BUCKET_NAME: str = Field(default="rag-documents")
    MINIO_SECURE: bool = False
    PRESIGNED_URL_EXPIRE_SECONDS: int = Field(default=3600, ge=60, le=86400)


class VectorConfig(BaseModel):
    QDRANT_HOST: str = Field(default="127.0.0.1")
    QDRANT_PORT: int = Field(default=6333, ge=1, le=65535)
    QDRANT_COLLECTION: str = Field(default="rag_chunks")
    VECTOR_DIMENSION: int = Field(default=2048, description="Vector dimensions (e.g. 1024, 1536, 2048)")

    @field_validator("VECTOR_DIMENSION")
    @classmethod
    def validate_dim(cls, v: int) -> int:
        if v <= 0 or v > 8192:
            raise ValueError("VECTOR_DIMENSION must be between 1 and 8192")
        return v


class GraphConfig(BaseModel):
    MEMGRAPH_HOST: str = Field(default="127.0.0.1")
    MEMGRAPH_PORT: int = Field(default=7687, ge=1, le=65535)
    MEMGRAPH_USER: Optional[str] = ""
    MEMGRAPH_PASSWORD: Optional[str] = ""
    QUERY_TIMEOUT_SECONDS: int = Field(default=30, ge=5, le=120)


class ModelConfig(BaseModel):
    USE_MOCK_MODELS: bool = False
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    LLM_MODEL: str = "glm-5.3-flash"
    EMBEDDING_MODEL: str = "embedding-3"
    LLM_TEMPERATURE: float = Field(default=0.3, ge=0.0, le=2.0)
    REQUEST_TIMEOUT_SECONDS: float = Field(default=60.0, ge=5.0, le=300.0)
    MINERU_ENABLED: bool = False
    MINERU_BASE_URL: Optional[str] = None


class ObservabilityConfig(BaseModel):
    LOG_LEVEL: str = Field(default="INFO", description="DEBUG, INFO, WARNING, ERROR")
    ENABLE_JSON_LOGS: bool = True
    MASK_SENSITIVE_DATA: bool = True
    MAX_LOG_MESSAGE_LENGTH: int = Field(default=4096, ge=256, le=65536)


class AppSettings(BaseSettings):
    """
    Root Application Configuration with Modular Sub-Settings.
    Loads from .env and enforces immutable snapshots.
    """
    model_config = SettingsConfigDict(
        # Resolve configuration relative to the project, not the process
        # working directory. This keeps API/Worker/scripts consistent when
        # launched from a supervisor or another directory.
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    APP_NAME: str = "rag-api-pro"
    APP_ENV: str = "development"
    DEBUG: bool = True
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    # Direct access flat properties for backward compatibility
    SECRET_KEY: str = "rag-pro-super-secret-key-change-in-production-2026"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    DATABASE_URL: str = "postgresql+asyncpg://rag_user:rag_password@127.0.0.1:5432/rag_pro"
    SYNC_DATABASE_URL: str = "postgresql://rag_user:rag_password@127.0.0.1:5432/rag_pro"
    POOL_SIZE: int = 20
    MAX_OVERFLOW: int = 10
    POOL_RECYCLE_SECONDS: int = 3600
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_STREAM_NAME: str = "stream:rag_tasks"
    REDIS_CONSUMER_GROUP: str = "cg:rag_workers"
    MINIO_ENDPOINT: str = "127.0.0.1:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin_secret"
    MINIO_BUCKET_NAME: str = "rag-documents"
    MINIO_SECURE: bool = False
    QDRANT_HOST: str = "127.0.0.1"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "rag_chunks"
    VECTOR_DIMENSION: int = 2048
    MEMGRAPH_HOST: str = "127.0.0.1"
    MEMGRAPH_PORT: int = 7687
    MEMGRAPH_USER: Optional[str] = ""
    MEMGRAPH_PASSWORD: Optional[str] = ""
    USE_MOCK_MODELS: bool = False
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    LLM_MODEL: str = "glm-5.3-flash"
    EMBEDDING_MODEL: str = "embedding-3"
    MINERU_ENABLED: bool = False
    MINERU_BASE_URL: Optional[str] = None
    MINERU_API_KEY: Optional[str] = None
    LOG_LEVEL: str = "INFO"
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173"
    SEED_DEMO_DATA: bool = True

    @model_validator(mode="after")
    def validate_runtime_safety(self) -> "AppSettings":
        """Reject development-only defaults when running in production."""
        if self.APP_ENV.lower() in {"prod", "production"}:
            if self.SECRET_KEY == "rag-pro-super-secret-key-change-in-production-2026":
                raise ValueError("SECRET_KEY must be explicitly configured in production")
            if self.DEBUG:
                raise ValueError("DEBUG must be false in production")
            if self.USE_MOCK_MODELS:
                raise ValueError("USE_MOCK_MODELS must be false in production")
            if self.MINIO_ACCESS_KEY == "minioadmin" or self.MINIO_SECRET_KEY == "minioadmin_secret":
                raise ValueError("MinIO credentials must be explicitly configured in production")
            if "rag_password" in self.DATABASE_URL:
                raise ValueError("Database credentials must be explicitly configured in production")
            if not self.CORS_ALLOWED_ORIGINS.strip() or "*" in self.CORS_ALLOWED_ORIGINS:
                raise ValueError("CORS_ALLOWED_ORIGINS must be explicit in production")
            if self.SEED_DEMO_DATA:
                raise ValueError("SEED_DEMO_DATA must be false in production")
        return self

    @property
    def security(self) -> SecurityConfig:
        return SecurityConfig(
            SECRET_KEY=self.SECRET_KEY,
            JWT_ALGORITHM=self.JWT_ALGORITHM,
            ACCESS_TOKEN_EXPIRE_MINUTES=self.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    @property
    def database(self) -> DatabaseConfig:
        return DatabaseConfig(
            DATABASE_URL=self.DATABASE_URL,
            SYNC_DATABASE_URL=self.SYNC_DATABASE_URL,
            POOL_SIZE=self.POOL_SIZE,
            MAX_OVERFLOW=self.MAX_OVERFLOW,
            POOL_RECYCLE_SECONDS=self.POOL_RECYCLE_SECONDS,
        )

    @property
    def redis(self) -> RedisConfig:
        return RedisConfig(
            REDIS_URL=self.REDIS_URL,
            REDIS_STREAM_NAME=self.REDIS_STREAM_NAME,
            REDIS_CONSUMER_GROUP=self.REDIS_CONSUMER_GROUP
        )

    @property
    def minio(self) -> MinioConfig:
        return MinioConfig(
            MINIO_ENDPOINT=self.MINIO_ENDPOINT,
            MINIO_ACCESS_KEY=self.MINIO_ACCESS_KEY,
            MINIO_SECRET_KEY=self.MINIO_SECRET_KEY,
            MINIO_BUCKET_NAME=self.MINIO_BUCKET_NAME,
            MINIO_SECURE=self.MINIO_SECURE
        )

    @property
    def vector(self) -> VectorConfig:
        return VectorConfig(
            QDRANT_HOST=self.QDRANT_HOST,
            QDRANT_PORT=self.QDRANT_PORT,
            QDRANT_COLLECTION=self.QDRANT_COLLECTION,
            VECTOR_DIMENSION=self.VECTOR_DIMENSION
        )

    @property
    def graph(self) -> GraphConfig:
        return GraphConfig(
            MEMGRAPH_HOST=self.MEMGRAPH_HOST,
            MEMGRAPH_PORT=self.MEMGRAPH_PORT,
            MEMGRAPH_USER=self.MEMGRAPH_USER,
            MEMGRAPH_PASSWORD=self.MEMGRAPH_PASSWORD
        )

    @property
    def models(self) -> ModelConfig:
        return ModelConfig(
            USE_MOCK_MODELS=self.USE_MOCK_MODELS,
            OPENAI_API_KEY=self.OPENAI_API_KEY,
            OPENAI_BASE_URL=self.OPENAI_BASE_URL,
            LLM_MODEL=self.LLM_MODEL,
            EMBEDDING_MODEL=self.EMBEDDING_MODEL,
            MINERU_ENABLED=self.MINERU_ENABLED,
            MINERU_BASE_URL=self.MINERU_BASE_URL
        )

    @property
    def observability(self) -> ObservabilityConfig:
        return ObservabilityConfig(
            LOG_LEVEL=self.LOG_LEVEL
        )


# Immutable singleton instance
settings = AppSettings()


def get_settings() -> AppSettings:
    """Returns the immutable settings snapshot."""
    return settings
