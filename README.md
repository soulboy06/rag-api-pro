# RAG API Pro

A production-oriented, multi-tenant GraphRAG platform. It chains document upload, async parsing, chunking,
vector search, keyword search, graph retrieval, and LLM-powered QA into one complete pipeline — with a React admin UI.

**Live demo:** https://rag.soulboy.xyz

## What it does

- Multi-tenant login, RBAC, and knowledge-base isolation
- PDF / DOCX / TXT upload and parsing
- MinIO for raw files, PostgreSQL for business metadata and task state
- Redis Streams for dispatching async processing jobs
- Qdrant for vectors, Memgraph for entity relations
- Hybrid retrieval, GraphRAG local/global retrieval, SSE streaming QA
- Index health checks, task retry, lease recovery, idempotency, storage reconciliation
- One `docker compose` brings up frontend, API, worker, and infra

## Architecture

```
Browser
   │
   ▼
Nginx + React :3000 ──► FastAPI :8000
                              │
           ┌──────────────────┼──────────────────┐
           ▼                  ▼                  ▼
       PostgreSQL        Redis Streams         MinIO
      source of truth    job dispatch       raw files
           │                  │                  │
           └──────────────► Worker ◄─────────────┘
                                │
                      ┌─────────┴─────────┐
                      ▼                   ▼
                   Qdrant              Memgraph
                vector index         graph index
```

## Quickstart

Requires Docker Engine + Docker Compose.

```bash
cp .env.example .env
# edit .env: set SECRET_KEY, POSTGRES_PASSWORD, MINIO_SECRET_KEY

docker compose up -d --build
```

- App: http://localhost:3000
- API docs: http://localhost:8000/docs
- Logs: `docker compose logs -f api worker`

Dev mode uses mock models so you can verify the full pipeline without keys. For real models, set
`USE_MOCK_MODELS=false` and fill in `OPENAI_API_KEY`, base URL, and model name. MinerU can be enabled
for complex PDFs; turn it off for native-text PDFs.

## Key design decisions

- **Postgres is the source of truth; Redis only carries events.** Uploads commit the business record +
  outbox event in Postgres first, then publish to Redis asynchronously. If Redis is down, events are
  retained and retried.
- **Qdrant and Memgraph are rebuildable secondary indexes.** A document is only shown as "searchable"
  when task state, active index version, and actual vector counts in Qdrant all agree.
- **Complexity-driven parser routing.** Each document gets a complexity score (page count, image count,
  text layer presence) that selects MinerU, VLM OCR, or PyMuPDF — with automatic fallback along the chain.
- **Tenant isolation at runtime.** A `TenantScope` carries tenant / KB / user / role context; versioned
  Pydantic configs plus immutable `PromptSnapshot`s prevent cross-tenant config and prompt leakage.

## Layout

```
src/api/       FastAPI routes and app entry
src/services/  document, task, and RAG services
src/parsers/   parsing, chunking, and parser routing
src/core/      DB, auth, rate limiting, task reliability, reconciliation
worker/        async document-processing worker
frontend/      React/Vite pages + production Nginx config
scripts/       init, reconciliation, backup, ops scripts
```

## Scope note

This is a runnable reference deployment, not a complete production platform. Before production use, add
proper DB migrations, HTTPS, secret management, backup drills, monitoring/alerting, load testing, and
stricter access policies.
