"""
Level 0: Pydantic Schemas for Standard Domain Contracts
Defines API request/response models, Task contracts, and SSE event streaming protocols.
Fixes: P1-API-01, P3-CODE-01
"""
from datetime import datetime
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field


# ---------------- Auth Schemas ----------------
class UserLogin(BaseModel):
    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")
    tenant_name: Optional[str] = Field(default="default", description="Tenant name")


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int = Field(..., ge=1)
    token_type: str = "bearer"
    user_id: str
    tenant_id: str
    username: str
    role: str


class UserInfo(BaseModel):
    id: str
    tenant_id: str
    username: str
    role: str
    created_at: datetime


# ---------------- Document & Task Schemas ----------------
class DocumentInfo(BaseModel):
    id: str
    tenant_id: str
    kb_id: str
    filename: str
    content_type: str
    file_size: int
    content_hash: Optional[str] = None
    task_status: Optional[str] = "SUCCEEDED"
    task_stage: Optional[str] = "COMPLETED"
    task_progress: Optional[int] = 100
    task_id: Optional[str] = None
    error_msg: Optional[str] = None
    active_generation: int = 1
    # This is derived from the actual vector index, not inferred only from
    # the historical task status.  It prevents a stale PostgreSQL record from
    # being shown as searchable after a derived-index volume is lost.
    index_status: str = "UNKNOWN"
    indexed_chunks: Optional[int] = None
    created_at: datetime


class TaskInfo(BaseModel):
    id: str
    tenant_id: str
    kb_id: str
    doc_id: str
    task_type: str
    status: str
    stage: str = "INIT"
    progress_percent: int = 0
    attempt: int = 0
    retry_count: int = 0
    fencing_token: int = 1
    idempotency_key: Optional[str] = None
    worker_id: Optional[str] = None
    error_msg: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    next_attempt_at: Optional[datetime] = None
    target_generation: int = 1


class UploadResponse(BaseModel):
    document: DocumentInfo
    task: TaskInfo
    message: str = "Document uploaded successfully and ingestion task queued"


# ---------------- RAG & Query Schemas ----------------
class SourceChunk(BaseModel):
    doc_id: str
    filename: str
    chunk_index: int
    content: str
    score: float
    page_number: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


class GraphEntity(BaseModel):
    name: str
    label: str = "Entity"
    relations: List[str] = []


class QueryRequest(BaseModel):
    kb_id: str = Field(..., description="Knowledge Base ID to query")
    query: str = Field(..., min_length=1, max_length=2048, description="User question")
    mode: Optional[Literal["naive", "local", "global", "hybrid", "mix"]] = Field(default=None, description="Retrieval mode")
    top_k: Optional[int] = Field(default=None, ge=1, le=20, description="Top chunks to retrieve")
    stream: bool = Field(default=False, description="Enable SSE token streaming")
    session_id: Optional[str] = Field(default=None, description="Optional Chat Session ID for multi-turn conversations")
    score_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Minimum relevance score threshold")


class QueryResponse(BaseModel):
    query: str
    answer: str
    mode: str
    sources: List[SourceChunk] = []
    entities: List[GraphEntity] = []
    execution_time_ms: float = 0.0


# ---------------- Safe SSE Streaming Event Protocols ----------------
class SSEStatusEvent(BaseModel):
    event: Literal["status"] = "status"
    stage: str
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SSETokenEvent(BaseModel):
    event: Literal["token"] = "token"
    delta: str


class SSESourcesEvent(BaseModel):
    event: Literal["sources"] = "sources"
    sources: List[SourceChunk]
    entities: List[GraphEntity] = Field(default_factory=list)
    count: int = 0


class SSEErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    error_code: str
    message: str
    request_id: Optional[str] = None


class SSEDoneEvent(BaseModel):
    event: Literal["done"] = "done"
    total_tokens: Optional[int] = None
    execution_time_ms: float = 0.0
    status: Literal["COMPLETED", "FAILED"] = "COMPLETED"


# ---------------- Audit & Session Schemas ----------------
class AuditLogSchema(BaseModel):
    id: str
    tenant_id: str
    user_id: Optional[str]
    request_id: str
    endpoint: str
    method: str
    status_code: int
    latency_ms: float
    client_ip: Optional[str]
    created_at: datetime
