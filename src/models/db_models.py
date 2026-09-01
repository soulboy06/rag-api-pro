"""
Level 0: SQLAlchemy ORM Models for PostgreSQL Business Fact Source
Defines tenants, users, knowledge bases, documents, tasks, chat sessions, tenant configs, prompts, and audit logs.
Fixes: P1-API-01, P3-CODE-01
"""
from datetime import datetime, timezone
import uuid
from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    BigInteger,
    Float,
    DateTime,
    ForeignKey,
    JSON,
    Index,
    Boolean,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from src.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_uuid() -> str:
    return str(uuid.uuid4())


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    name = Column(String(128), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    knowledge_bases = relationship("KnowledgeBase", back_populates="tenant", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="tenant", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="tenant", cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="tenant", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="tenant")
    tenant_configs = relationship("TenantConfig", back_populates="tenant", cascade="all, delete-orphan")
    tenant_prompts = relationship("TenantPrompt", back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    username = Column(String(64), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(32), default="MEMBER", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    refresh_token_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant = relationship("Tenant", back_populates="users")
    chat_sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_users_tenant_username", "tenant_id", "username", unique=True),
    )


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant = relationship("Tenant", back_populates="knowledge_bases")
    documents = relationship("Document", back_populates="knowledge_base", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="knowledge_base", cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="knowledge_base", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_kb_tenant_name", "tenant_id", "name", unique=True),
    )


class Document(Base):
    __tablename__ = "documents"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    kb_id = Column(String(64), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(128), nullable=False)
    file_size = Column(BigInteger, nullable=False)
    minio_bucket = Column(String(128), nullable=False)
    minio_key = Column(String(512), nullable=False)
    content_hash = Column(String(64), nullable=True, index=True)
    # Only this generation is visible to retrieval.  New generations are
    # built beside the active one and become visible in one database commit.
    active_generation = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant = relationship("Tenant", back_populates="documents")
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    tasks = relationship("Task", back_populates="document", cascade="all, delete-orphan")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    kb_id = Column(String(64), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True)
    doc_id = Column(String(64), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    task_type = Column(String(64), default="DOCUMENT_INGESTION", nullable=False)
    status = Column(String(32), default="PENDING", nullable=False, index=True)
    stage = Column(String(64), default="INIT", nullable=False)
    progress_percent = Column(Integer, default=0, nullable=False)
    attempt = Column(Integer, default=0, nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)
    fencing_token = Column(BigInteger, default=1, nullable=False)
    idempotency_key = Column(String(128), nullable=True, index=True)
    worker_id = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    task_options = Column(JSON, nullable=True)
    target_generation = Column(Integer, default=1, nullable=False)
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    tenant = relationship("Tenant", back_populates="tasks")
    knowledge_base = relationship("KnowledgeBase", back_populates="tasks")
    document = relationship("Document", back_populates="tasks")

    __table_args__ = (
        Index("ix_tasks_tenant_kb_status", "tenant_id", "kb_id", "status"),
        Index("ix_tasks_idempotency", "tenant_id", "idempotency_key"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tasks_tenant_idempotency"),
    )


class TaskOutboxEvent(Base):
    """Durable task-delivery event written in the same transaction as task state."""
    __tablename__ = "task_outbox_events"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    event_key = Column(String(160), nullable=False, unique=True, index=True)
    task_id = Column(String(64), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=False)
    status = Column(String(32), default="PENDING", nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    available_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_task_outbox_delivery", "status", "available_at"),
    )

    @property
    def next_attempt_at(self):
        """Compatibility alias for callers that describe delivery as retry timing."""
        return self.available_at


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    kb_id = Column(String(64), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), default="新对话", nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    tenant = relationship("Tenant", back_populates="chat_sessions")
    knowledge_base = relationship("KnowledgeBase", back_populates="chat_sessions")
    user = relationship("User", back_populates="chat_sessions")
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    session_id = Column(String(64), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(32), nullable=False)  # user / assistant / system
    content = Column(Text, nullable=False)
    sources = Column(JSON, nullable=True)  # List of SourceChunk dictionaries
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    session = relationship("ChatSession", back_populates="messages")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(String(64), nullable=True, index=True)
    request_id = Column(String(64), nullable=False, index=True)
    endpoint = Column(String(255), nullable=False)
    method = Column(String(16), nullable=False)
    status_code = Column(Integer, nullable=False)
    latency_ms = Column(Float, nullable=False)
    client_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant = relationship("Tenant", back_populates="audit_logs")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    key_prefix = Column(String(16), nullable=False)  # e.g. "rk_live_a1b2"
    key_hash = Column(String(64), nullable=False, unique=True, index=True)  # SHA-256
    role = Column(String(32), default="MEMBER", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    tenant = relationship("Tenant")
    user = relationship("User")


class TenantConfig(Base):
    __tablename__ = "tenant_configs"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    version_id = Column(Integer, default=1, nullable=False)
    config_data = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    tenant = relationship("Tenant", back_populates="tenant_configs")

    __table_args__ = (
        Index("ix_tenant_config_version", "tenant_id", "version_id", unique=True),
    )


class TenantPrompt(Base):
    __tablename__ = "tenant_prompts"

    id = Column(String(64), primary_key=True, default=generate_uuid)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    kb_id = Column(String(64), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True, index=True)
    qa_template = Column(Text, nullable=True)
    entity_template = Column(Text, nullable=True)
    query_rewrite_template = Column(Text, nullable=True)
    custom_persona = Column(Text, nullable=True)
    custom_entities = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    tenant = relationship("Tenant", back_populates="tenant_prompts")
    knowledge_base = relationship("KnowledgeBase")

    __table_args__ = (
        Index("ix_tenant_prompts_scope", "tenant_id", "kb_id", unique=True),
    )
