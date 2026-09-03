"""
Level 6: RAG Service with GraphRAG 5 Retrieval Paradigms & SSE Streaming
Supports: naive, local, global, hybrid, mix modes with token rate limiting and SSE streaming.
Fixes: P0-REL-03, P1-API-02, P1-API-03, P1-API-04, P1-REMOTE-05, P3-CODE-04
"""
import time
import hashlib
import json
from typing import List, Dict, Any, Optional, AsyncGenerator, Tuple
import httpx
from sqlalchemy import select

from src.core.config import settings
from src.core.clients import InfrastructureClients
from src.core.ratelimit import (
    TokenCounter,
    DualWindowRateLimiter,
    global_rate_limiter,
    metrics_collector,
)
from src.core.monitoring.metrics import prometheus_metrics
from src.models.schemas import QueryRequest, QueryResponse, SourceChunk, GraphEntity
from src.models.db_models import KnowledgeBase, Document
from src.core.exceptions import ResourceNotFoundError
from src.core.tenant.prompts import TenantPromptManager
from src.core.tenant.config_manager import TenantConfigManager
from src.services.rag.engine import GraphRAGEngine
from src.services.rag.stream import SSEStreamGenerator
from src.services.rag.chat_session import ChatSessionManager


class RAGService:
    @staticmethod
    def _generate_mock_embedding(text: str, dim: int = 1024) -> List[float]:
        """Generate a deterministic normalized pseudo-embedding for testing."""
        hasher = hashlib.sha256(text.encode("utf-8"))
        seed = int(hasher.hexdigest(), 16)
        vec = []
        for i in range(dim):
            val = ((seed >> (i % 64)) & 0xFF) / 255.0 - 0.5
            vec.append(val)
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]

    @classmethod
    async def get_embedding(
        cls,
        text: str,
        tenant_id: str = "global",
        model: Optional[str] = None,
    ) -> List[float]:
        if model:
            res = await cls.get_batch_embeddings([text], tenant_id=tenant_id, model=model)
        else:
            res = await cls.get_batch_embeddings([text], tenant_id=tenant_id)
        return res[0] if res else cls._generate_mock_embedding(text, settings.VECTOR_DIMENSION)

    @classmethod
    async def get_batch_embeddings(
        cls,
        texts: List[str],
        tenant_id: str = "global",
        model: Optional[str] = None,
    ) -> List[List[float]]:
        if not texts:
            return []
        total_tokens = sum(TokenCounter.estimate_text_tokens(t) for t in texts)
        await global_rate_limiter.acquire(
            tenant_id=tenant_id,
            estimated_tokens=total_tokens,
            service="embedding",
        )

        if settings.USE_MOCK_MODELS or not settings.OPENAI_API_KEY:
            metrics_collector.record_success(tokens=total_tokens)
            return [cls._generate_mock_embedding(t, settings.VECTOR_DIMENSION) for t in texts]

        client = await InfrastructureClients.get_http_client()
        headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}
        payload = {
            "input": texts,
            "model": model or settings.EMBEDDING_MODEL,
        }
        res = await client.post(
            f"{settings.OPENAI_BASE_URL}/embeddings",
            headers=headers,
            json=payload,
            timeout=60.0,
        )
        res.raise_for_status()
        data = res.json()
        items = data.get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            raise RuntimeError("Embedding provider returned an incomplete data array")
        items = sorted(items, key=lambda x: x.get("index", 0))
        embeddings = [item.get("embedding") for item in items]
        if any(not isinstance(embedding, list) for embedding in embeddings):
            raise RuntimeError("Embedding provider returned an invalid embedding vector")
        metrics_collector.record_success(tokens=total_tokens)
        return embeddings

    @classmethod
    async def generate_completion(
        cls,
        prompt: str,
        system_prompt: str,
        tenant_id: str = "global",
        model: Optional[str] = None,
        temperature: float = 0.3,
    ) -> str:
        full_text = f"{system_prompt}\n{prompt}"
        est_tokens = TokenCounter.estimate_text_tokens(full_text)
        await global_rate_limiter.acquire(
            tenant_id=tenant_id,
            estimated_tokens=est_tokens,
            service="llm",
        )

        if settings.USE_MOCK_MODELS or not settings.OPENAI_API_KEY:
            metrics_collector.record_success(tokens=est_tokens)
            return f"【系统回答】根据知识库与图谱检索到的事实依据：\n{prompt[:300]}...\n以上由 RAG Pro 引擎结合向量与图谱融合生成。"

        client = await InfrastructureClients.get_http_client()
        headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}
        payload = {
            "model": model or settings.LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature,
        }
        res = await client.post(
            f"{settings.OPENAI_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60.0,
        )
        res.raise_for_status()
        data = res.json()
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("LLM provider returned no choices")
        message = choices[0].get("message")
        completion_text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(completion_text, str):
            raise RuntimeError("LLM provider returned invalid completion content")
        usage = TokenCounter.parse_llm_response_usage(
            data,
            fallback_prompt_text=full_text,
            fallback_completion_text=completion_text
        )
        metrics_collector.record_success(tokens=usage.total_tokens)
        prometheus_metrics.record_token_usage(
            tenant_id=tenant_id,
            model=model or settings.LLM_MODEL,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens
        )
        return completion_text

    @staticmethod
    async def _load_active_generations(db, tenant_id: str, kb_id: str) -> Dict[str, int]:
        """Loads the document generation map used to hide staging indexes."""
        rows = (
            await db.execute(
                select(Document.id, Document.active_generation).where(
                    Document.tenant_id == tenant_id,
                    Document.kb_id == kb_id,
                )
            )
        ).all()
        return {doc_id: (generation or 1) for doc_id, generation in rows}

    @classmethod
    async def execute_retrieval(
        cls,
        tenant_id: str,
        kb_id: str,
        query: str,
        mode: str = "hybrid",
        top_k: int = 5,
        score_threshold: float = 0.3,
        active_generations: Optional[Dict[str, int]] = None,
        embedding_model: Optional[str] = None,
    ) -> Tuple[List[SourceChunk], List[GraphEntity], List[str]]:
        """
        Executes retrieval according to the selected mode (naive, local, global, hybrid, mix).
        Returns: (sources, entities, context_strings)
        """
        mode = mode.lower()
        query_vec = await cls.get_embedding(
            query,
            tenant_id=tenant_id,
            model=embedding_model,
        )

        sources: List[SourceChunk] = []
        entities: List[GraphEntity] = []
        context_parts: List[str] = []

        # 1. NAIVE: Vector Search Only
        if mode == "naive":
            sources = await GraphRAGEngine.retrieve_vector_chunks(
                tenant_id, kb_id, query_vec, top_k, score_threshold, active_generations
            )
            for c in sources:
                page_str = f" (第{c.page_number}页)" if c.page_number else ""
                context_parts.append(f"[{c.filename}{page_str}#chunk_{c.chunk_index}]: {c.content}")

        # 2. LOCAL: Graph Subgraph Neighborhood Traversal Only
        elif mode == "local":
            entities, relations = await GraphRAGEngine.retrieve_local_subgraph(
                tenant_id, kb_id, query, limit=15, active_generations=active_generations
            )
            if relations:
                context_parts.append("【局部实体关系子图】:\n" + "\n".join(relations))

        # 3. GLOBAL: Community Summaries Only
        elif mode == "global":
            communities = await GraphRAGEngine.retrieve_global_communities(
                tenant_id, kb_id, limit=5, active_generations=active_generations
            )
            context_parts.append("【知识图谱全局社区主题摘要】:\n" + "\n".join(communities))

        # 4. HYBRID: Tri-Hybrid (Dense Vector + Sparse Keywords + Local Graph + RRF)
        elif mode == "hybrid":
            keywords = GraphRAGEngine.extract_search_keywords(query)
            dense_sources = await GraphRAGEngine.retrieve_vector_chunks(
                tenant_id,
                kb_id,
                query_vec,
                top_k=max(15, top_k * 2),
                score_threshold=score_threshold,
                active_generations=active_generations,
            )
            sparse_sources = await GraphRAGEngine.retrieve_sparse_keyword_chunks(
                tenant_id, kb_id, query, limit=15, active_generations=active_generations
            )
            entities, relations = await GraphRAGEngine.retrieve_local_subgraph(
                tenant_id, kb_id, query, limit=15, active_generations=active_generations
            )
            sources = GraphRAGEngine.reciprocal_rank_fusion(
                dense_sources=dense_sources,
                sparse_sources=sparse_sources,
                graph_relations=relations,
                keywords=keywords,
                top_k=top_k
            )
            sources = GraphRAGEngine.rerank_sources(sources, query, top_k)
            for c in sources:
                page_str = f" (第{c.page_number}页)" if c.page_number else ""
                context_parts.append(f"[{c.filename}{page_str}#chunk_{c.chunk_index}]: {c.content}")
            if relations:
                context_parts.append("【局部实体关系子图】:\n" + "\n".join(relations))

        # 5. MIX: Global Communities + Local Entities + Sparse Keywords + Vectors + RRF
        elif mode == "mix":
            keywords = GraphRAGEngine.extract_search_keywords(query)
            dense_sources = await GraphRAGEngine.retrieve_vector_chunks(
                tenant_id,
                kb_id,
                query_vec,
                top_k=max(15, top_k * 2),
                score_threshold=score_threshold,
                active_generations=active_generations,
            )
            sparse_sources = await GraphRAGEngine.retrieve_sparse_keyword_chunks(
                tenant_id, kb_id, query, limit=15, active_generations=active_generations
            )
            entities, relations = await GraphRAGEngine.retrieve_local_subgraph(
                tenant_id, kb_id, query, limit=15, active_generations=active_generations
            )
            communities = await GraphRAGEngine.retrieve_global_communities(
                tenant_id, kb_id, limit=3, active_generations=active_generations
            )
            sources = GraphRAGEngine.reciprocal_rank_fusion(
                dense_sources=dense_sources,
                sparse_sources=sparse_sources,
                graph_relations=relations,
                keywords=keywords,
                top_k=top_k
            )
            sources = GraphRAGEngine.rerank_sources(sources, query, top_k)
            for c in sources:
                page_str = f" (第{c.page_number}页)" if c.page_number else ""
                context_parts.append(f"[{c.filename}{page_str}#chunk_{c.chunk_index}]: {c.content}")
            if relations:
                context_parts.append("【局部实体关系子图】:\n" + "\n".join(relations))
            if communities:
                context_parts.append("【全局社区主题】:\n" + "\n".join(communities))

        else:
            # Fallback to hybrid
            return await cls.execute_retrieval(
                tenant_id,
                kb_id,
                query,
                mode="hybrid",
                top_k=top_k,
                score_threshold=score_threshold,
                active_generations=active_generations,
                embedding_model=embedding_model,
            )

        # Record retrieval metrics
        prometheus_metrics.retrieval_chunks_total.labels(
            tenant_id=tenant_id or "default",
            mode=mode
        ).inc(len(sources))

        return sources, entities, context_parts

    @classmethod
    async def query_hybrid(
        cls,
        request: QueryRequest,
        tenant_id: str,
        db=None,
        user_id: Optional[str] = None,
    ) -> QueryResponse:
        start_time = time.perf_counter()

        if db:
            kb = (
                await db.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.id == request.kb_id,
                        KnowledgeBase.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if not kb:
                raise ResourceNotFoundError(
                    f"Knowledge Base '{request.kb_id}' not found for tenant"
                )
            active_generations = await cls._load_active_generations(
                db, tenant_id, request.kb_id
            )
        else:
            active_generations = None

        # Contextualize multi-turn query if session_id is present
        if db and request.session_id:
            await ChatSessionManager.load_history_from_db(
                db,
                request.session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                kb_id=request.kb_id,
            )
        effective_query = ChatSessionManager.contextualize_query(request.session_id, request.query)
        if request.session_id:
            if db:
                await ChatSessionManager.append_message_db(
                    db,
                    request.session_id,
                    "user",
                    request.query,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    kb_id=request.kb_id,
                )
            else:
                ChatSessionManager.append_message(request.session_id, "user", request.query)
        # 1. Execute Multi-Paradigm Retrieval
        cfg = (
            await TenantConfigManager.load_latest_config_db(db, tenant_id)
            if db
            else TenantConfigManager.get_latest_config(tenant_id)
        )
        effective_top_k = request.top_k if request.top_k is not None else cfg.top_k
        effective_score_threshold = request.score_threshold if request.score_threshold is not None else cfg.score_threshold
        effective_mode = request.mode or cfg.retrieval_mode

        sources, entities, context_parts = await cls.execute_retrieval(
            tenant_id=tenant_id,
            kb_id=request.kb_id,
            query=effective_query,
            mode=effective_mode,
            top_k=effective_top_k,
            score_threshold=effective_score_threshold,
            active_generations=active_generations,
            embedding_model=cfg.embedding_model,
        )

        # 2. Assemble Dynamic Isolated Prompt Snapshot
        prompt_snapshot = (
            await TenantPromptManager.load_snapshot_db(db, tenant_id, request.kb_id)
            if db
            else TenantPromptManager.get_snapshot(tenant_id=tenant_id, kb_id=request.kb_id)
        )
        combined_context = "\n\n".join(context_parts) if context_parts else "未检索到相关知识库文档或图谱关系。"

        rendered_prompt = prompt_snapshot.render_qa(context=combined_context, question=effective_query)
        system_prompt = "You are an enterprise knowledge base assistant. Answer factually based on context."
        persona = prompt_snapshot.custom_persona or cfg.system_persona
        if persona:
            system_prompt = f"{persona}\n\n{system_prompt}"

        # 3. Generate Answer
        answer = await cls.generate_completion(
            rendered_prompt,
            system_prompt,
            tenant_id=tenant_id,
            model=cfg.llm_model,
            temperature=cfg.temperature,
        )
        execution_time_ms = round((time.perf_counter() - start_time) * 1000, 2)

        # Record into chat history if session_id provided
        if request.session_id:
            if db:
                await ChatSessionManager.append_message_db(
                    db,
                    request.session_id,
                    "assistant",
                    answer,
                    sources=[source.model_dump() for source in sources],
                    tenant_id=tenant_id,
                    user_id=user_id,
                    kb_id=request.kb_id,
                )
            else:
                ChatSessionManager.append_message(request.session_id, "assistant", answer)

        return QueryResponse(
            query=request.query,
            answer=answer,
            mode=effective_mode,
            sources=sources,
            entities=entities,
            execution_time_ms=execution_time_ms,
        )

    @classmethod
    async def query_stream(
        cls,
        request: QueryRequest,
        tenant_id: str,
        db=None,
        user_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        start_time = time.perf_counter()
        if db:
            kb = (
                await db.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.id == request.kb_id,
                        KnowledgeBase.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if not kb:
                raise ResourceNotFoundError(
                    f"Knowledge Base '{request.kb_id}' not found for tenant"
                )
            active_generations = await cls._load_active_generations(
                db, tenant_id, request.kb_id
            )
        else:
            active_generations = None
        if db and request.session_id:
            await ChatSessionManager.load_history_from_db(
                db,
                request.session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                kb_id=request.kb_id,
            )
        effective_query = ChatSessionManager.contextualize_query(request.session_id, request.query)
        if request.session_id:
            if db:
                await ChatSessionManager.append_message_db(
                    db,
                    request.session_id,
                    "user",
                    request.query,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    kb_id=request.kb_id,
                )
            else:
                ChatSessionManager.append_message(request.session_id, "user", request.query)
        # 1. Execute Retrieval
        cfg = (
            await TenantConfigManager.load_latest_config_db(db, tenant_id)
            if db
            else TenantConfigManager.get_latest_config(tenant_id)
        )
        effective_top_k = request.top_k if request.top_k is not None else cfg.top_k
        effective_score_threshold = request.score_threshold if request.score_threshold is not None else cfg.score_threshold
        effective_mode = request.mode or cfg.retrieval_mode

        sources, entities, context_parts = await cls.execute_retrieval(
            tenant_id=tenant_id,
            kb_id=request.kb_id,
            query=effective_query,
            mode=effective_mode,
            top_k=effective_top_k,
            score_threshold=effective_score_threshold,
            active_generations=active_generations,
            embedding_model=cfg.embedding_model,
        )

        # 2. Assemble Dynamic Isolated Prompt Snapshot
        prompt_snapshot = (
            await TenantPromptManager.load_snapshot_db(db, tenant_id, request.kb_id)
            if db
            else TenantPromptManager.get_snapshot(tenant_id=tenant_id, kb_id=request.kb_id)
        )
        combined_context = "\n\n".join(context_parts) if context_parts else "未检索到相关知识库文档或图谱关系。"

        rendered_prompt = prompt_snapshot.render_qa(context=combined_context, question=effective_query)
        system_prompt = "You are an enterprise knowledge base assistant. Answer factually based on context."
        persona = prompt_snapshot.custom_persona or cfg.system_persona
        if persona:
            system_prompt = f"{persona}\n\n{system_prompt}"

        # 3. Stream SSE Generator
        raw_stream = SSEStreamGenerator.stream_query_response(
            prompt=rendered_prompt,
            system_prompt=system_prompt,
            sources=sources,
            entities=entities,
            tenant_id=tenant_id,
            model=cfg.llm_model,
            temperature=cfg.temperature,
            start_time=start_time
        )

        async def persist_stream():
            answer_parts = []
            stream_succeeded = True
            async for frame in raw_stream:
                if frame.startswith("event: token"):
                    try:
                        data_line = next(
                            line for line in frame.splitlines() if line.startswith("data: ")
                        )
                        answer_parts.append(json.loads(data_line[6:]).get("delta", ""))
                    except (StopIteration, json.JSONDecodeError, TypeError):
                        pass
                elif frame.startswith("event: error"):
                    stream_succeeded = False
                elif frame.startswith("event: done"):
                    try:
                        data_line = next(
                            line for line in frame.splitlines() if line.startswith("data: ")
                        )
                        if json.loads(data_line[6:]).get("status") != "COMPLETED":
                            stream_succeeded = False
                    except (StopIteration, json.JSONDecodeError, TypeError):
                        stream_succeeded = False
                yield frame

            if request.session_id:
                assistant_answer = "".join(answer_parts)
                # User messages are persisted before retrieval. Only persist
                # an assistant turn when the model completed successfully; a
                # failed stream must not create a misleading blank answer.
                if stream_succeeded and assistant_answer.strip() and db:
                    await ChatSessionManager.append_message_db(
                        db,
                        request.session_id,
                        "assistant",
                        assistant_answer,
                        sources=[source.model_dump() for source in sources],
                        tenant_id=tenant_id,
                        user_id=user_id,
                        kb_id=request.kb_id,
                    )
                elif stream_succeeded and assistant_answer.strip():
                    ChatSessionManager.append_message(
                        request.session_id,
                        "assistant",
                        assistant_answer,
                        sources=[source.model_dump() for source in sources],
                    )

        return persist_stream()
