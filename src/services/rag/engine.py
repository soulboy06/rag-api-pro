"""
Level 6: GraphRAG Multi-Paradigm Retrieval Engine with Tri-Hybrid Fusion
Implements 5 distinct retrieval paradigms:
1. naive: Dense vector similarity search (Qdrant)
2. local: Entity-centric 1-2 hop subgraph neighborhood traversal (Memgraph)
3. global: High-level community themes and structural summaries (Memgraph)
4. hybrid: Tri-Hybrid Reciprocal Rank Fusion (Dense Vector + Sparse Keyword Match + Graph Subgraphs)
5. mix: Comprehensive blend of global communities, local subgraphs, sparse keywords, dense vectors and reranking
Fixes: P1-API-02, P1-API-03, P1-API-04, P1-PARSER-09, P3-CODE-04
"""
import time
import re
from typing import List, Dict, Any, Optional, Tuple
import jieba
import jieba.analyse
from qdrant_client.http import models as qmodels

from src.core.config import settings
from src.core.clients import InfrastructureClients
from src.models.schemas import QueryRequest, QueryResponse, SourceChunk, GraphEntity
from src.core.tenant.prompts import TenantPromptManager, PromptSnapshot
from src.core.logger import get_logger

logger = get_logger(__name__)


class GraphRAGEngine:
    STOP_WORDS = {
        "根据", "请问", "请", "问", "什么", "多少", "在", "了", "的", "有哪些", "是多少", 
        "讲了", "怎么", "如何", "关于", "对于", "哪种", "哪些", "有关", "一个", "一份", "年", "月", "日",
        "公司", "报告", "年度", "告知", "提供", "介绍", "有多少", "获得", "获得了", "一款", "以及", "2024",
        "看", "从", "最", "最高", "最低", "该", "是什么", "是多少", "几款", "哪个", "业务"
    }

    @staticmethod
    def _source_from_payload(payload: Dict[str, Any], score: float) -> SourceChunk:
        metadata = dict(payload.get("metadata") or {})
        for key in ("heading", "page_numbers", "parser_used", "complexity_score", "generation"):
            if key in payload:
                metadata.setdefault(key, payload[key])
        return SourceChunk(
            doc_id=payload.get("doc_id", "unknown"),
            filename=payload.get("filename", "unknown"),
            chunk_index=payload.get("chunk_index", 0),
            content=payload.get("content", ""),
            score=round(float(score), 4),
            page_number=payload.get("page_number"),
            metadata=metadata,
        )

    @staticmethod
    def _tenant_kb_filter(
        tenant_id: str,
        kb_id: str,
        active_generations: Optional[Dict[str, int]] = None,
    ) -> qmodels.Filter:
        """Builds the mandatory tenant/KB scope and optional active-generation filter."""
        must = [
            qmodels.FieldCondition(key="tenant_id", match=qmodels.MatchValue(value=tenant_id)),
            qmodels.FieldCondition(key="kb_id", match=qmodels.MatchValue(value=kb_id)),
        ]
        if active_generations:
            generation_scopes = []
            for doc_id, generation in active_generations.items():
                generation_conditions = [
                    qmodels.FieldCondition(
                        key="doc_id",
                        match=qmodels.MatchValue(value=doc_id),
                    ),
                    qmodels.FieldCondition(
                        key="generation",
                        match=qmodels.MatchValue(value=generation),
                    ),
                ]
                # Points created before generation tracking are interpreted as
                # generation 1, preserving backward compatibility without
                # exposing a staged generation >= 2.
                if generation == 1:
                    generation_scopes.append(
                        qmodels.Filter(
                            must=[generation_conditions[0]],
                            should=[
                                generation_conditions[1],
                                qmodels.IsEmptyCondition(
                                    is_empty=qmodels.PayloadField(key="generation")
                                ),
                            ],
                        )
                    )
                else:
                    generation_scopes.append(qmodels.Filter(must=generation_conditions))
            must.append(
                qmodels.Filter(should=generation_scopes)
            )
        return qmodels.Filter(must=must)

    @classmethod
    def extract_search_keywords(cls, query_text: str) -> List[str]:
        """Extracts distinctive natural phrases and terms from query."""
        q_clean = re.sub(r"[《》]", "", query_text)
        
        # 1. Natural segment cut
        cut_words = [w.strip() for w in jieba.cut(q_clean) if len(w.strip()) >= 2 and w.strip() not in cls.STOP_WORDS]
        
        # 2. TF-IDF keywords
        tfidf_tags = [t for t in jieba.analyse.extract_tags(q_clean, topK=6) if t not in cls.STOP_WORDS]
        
        # 3. Adjacent natural bigrams
        bigrams = []
        for i in range(len(cut_words) - 1):
            w1, w2 = cut_words[i], cut_words[i+1]
            if w1 != w2 and len(w1 + w2) <= 10:
                bigrams.append(w1 + w2)
                
        candidates = list(dict.fromkeys(bigrams + tfidf_tags + cut_words))
        valid = [c for c in candidates if c not in cls.STOP_WORDS and len(c) >= 2]
        return valid[:8]

    @classmethod
    async def retrieve_vector_chunks(
        cls,
        tenant_id: str,
        kb_id: str,
        query_vec: List[float],
        top_k: int = 8,
        score_threshold: float = 0.3,
        active_generations: Optional[Dict[str, int]] = None,
    ) -> List[SourceChunk]:
        """Performs Dense Vector similarity search in Qdrant with candidate pool for RRF."""
        qdrant = InfrastructureClients.get_qdrant()

        search_filter = cls._tenant_kb_filter(tenant_id, kb_id, active_generations)

        candidate_limit = max(top_k * 4, 30)
        search_response = await qdrant.query_points(
            collection_name=settings.QDRANT_COLLECTION,
            query=query_vec,
            query_filter=search_filter,
            limit=candidate_limit,
        )

        sources: List[SourceChunk] = []
        for point in search_response.points:
            payload = point.payload or {}
            score_val = getattr(point, "score", 1.0) or 1.0
            if score_val >= score_threshold:
                sources.append(cls._source_from_payload(payload, score_val))
        return sources

    @classmethod
    async def retrieve_sparse_keyword_chunks(
        cls,
        tenant_id: str,
        kb_id: str,
        query_text: str,
        limit: int = 15,
        active_generations: Optional[Dict[str, int]] = None,
    ) -> List[SourceChunk]:
        """Performs sparse full-text keyword matching in Qdrant across content and search_index_text."""
        keywords = cls.extract_search_keywords(query_text)
        if not keywords:
            return []

        qdrant = InfrastructureClients.get_qdrant()
        seen_ids = set()
        matched_chunks: List[SourceChunk] = []

        for kw in keywords:
            try:
                scroll_res = await qdrant.scroll(
                    collection_name=settings.QDRANT_COLLECTION,
                    scroll_filter=qmodels.Filter(
                        must=[
                            cls._tenant_kb_filter(tenant_id, kb_id, active_generations),
                        ],
                        should=[
                            qmodels.FieldCondition(key="content", match=qmodels.MatchText(text=kw)),
                            qmodels.FieldCondition(key="search_index_text", match=qmodels.MatchText(text=kw)),
                        ]
                    ),
                    limit=limit,
                    with_payload=True
                )
                points = scroll_res[0]
                for pt in points:
                    pid = pt.id
                    if pid not in seen_ids:
                        seen_ids.add(pid)
                        p = pt.payload or {}
                        content = p.get("content", "")
                        search_idx = p.get("search_index_text", "")
                        combined = content + " " + search_idx
                        hit_weight = sum(len(k)**2 for k in keywords if k in combined)
                        is_table_boost = 2.0 if p.get("is_table") else 0.0
                        score = min(1.0, 0.5 + 0.1 * hit_weight + is_table_boost * 0.1)
                        matched_chunks.append(cls._source_from_payload(p, score))
            except Exception as exc:
                # Sparse retrieval is an optional branch of hybrid search;
                # preserve dense results but leave an observable diagnostic.
                logger.warning("Sparse retrieval branch failed", extra={"error": str(exc)})
                continue

        # Sort by keyword score
        matched_chunks.sort(key=lambda x: x.score, reverse=True)
        return matched_chunks[:limit]

    @classmethod
    async def retrieve_local_subgraph(
        cls,
        tenant_id: str,
        kb_id: str,
        query_text: str,
        max_hops: int = 2,
        limit: int = 15,
        active_generations: Optional[Dict[str, int]] = None,
    ) -> Tuple[List[GraphEntity], List[str]]:
        """Retrieves 1-2 hop neighborhood subgraph around entities mentioned in or related to the query."""
        keywords = cls.extract_search_keywords(query_text)
        memgraph = InfrastructureClients.get_memgraph()
        entities: List[GraphEntity] = []
        relation_triplets: List[str] = []

        try:
            async with memgraph.session() as session:
                # 1. Targeted Cypher query if keywords found
                if keywords:
                    cypher = """
                    MATCH (e:Entity {tenant_id: $tenant_id, kb_id: $kb_id})
                    WHERE any(kw IN $keywords WHERE e.name CONTAINS kw)
                    OPTIONAL MATCH (e)-[r:RELATED_TO]->(target:Entity)
                    RETURN e.name as entity_name, r.relation as rel_type, target.name as target_name, r.doc_id as doc_id, r.generation as generation
                    LIMIT $limit;
                    """
                    res = await session.run(cypher, tenant_id=tenant_id, kb_id=kb_id, keywords=keywords, limit=limit)
                else:
                    cypher = """
                    MATCH (e:Entity {tenant_id: $tenant_id, kb_id: $kb_id})
                    OPTIONAL MATCH (e)-[r:RELATED_TO]->(target:Entity)
                    RETURN e.name as entity_name, r.relation as rel_type, target.name as target_name, r.doc_id as doc_id, r.generation as generation
                    LIMIT $limit;
                    """
                    res = await session.run(cypher, tenant_id=tenant_id, kb_id=kb_id, limit=limit)

                records = await res.data()
                if active_generations:
                    records = [
                        record
                        for record in records
                        if record.get("doc_id") is None
                        or record.get("doc_id") not in active_generations
                        or record.get("generation") in {None, active_generations[record["doc_id"]]}
                    ]

                # Fallback if targeted query returned empty
                if not records:
                    fallback_cypher = """
                    MATCH (e:Entity {tenant_id: $tenant_id, kb_id: $kb_id})-[r:RELATED_TO]->(target:Entity)
                    RETURN e.name as entity_name, r.relation as rel_type, target.name as target_name, r.doc_id as doc_id, r.generation as generation
                    LIMIT $limit;
                    """
                    res = await session.run(fallback_cypher, tenant_id=tenant_id, kb_id=kb_id, limit=limit)
                    records = await res.data()
                    if active_generations:
                        records = [
                            record
                            for record in records
                            if record.get("doc_id") is None
                            or record.get("doc_id") not in active_generations
                            or record.get("generation") in {None, active_generations[record["doc_id"]]}
                        ]

                entity_map: Dict[str, List[str]] = {}
                for rec in records:
                    ename = rec.get("entity_name")
                    if not ename:
                        continue
                    tname = rec.get("target_name")
                    rel = rec.get("rel_type")
                    doc_id = rec.get("doc_id")
                    if ename not in entity_map:
                        entity_map[ename] = []
                    if tname and rel:
                        citation = f" [doc_id={doc_id}]" if doc_id else ""
                        rel_str = f"{ename} --[{rel}]--> {tname}{citation}"
                        entity_map[ename].append(rel_str)
                        relation_triplets.append(rel_str)

                for name, rels in entity_map.items():
                    entities.append(GraphEntity(name=name, label="Entity", relations=rels))
        except Exception as exc:
            logger.warning("Local graph retrieval branch failed", extra={"error": str(exc)})

        return entities, relation_triplets

    @classmethod
    async def retrieve_global_communities(
        cls,
        tenant_id: str,
        kb_id: str,
        limit: int = 5,
        active_generations: Optional[Dict[str, int]] = None,
    ) -> List[str]:
        """Retrieves high-level community themes and macro knowledge graph patterns."""
        memgraph = InfrastructureClients.get_memgraph()
        community_summaries: List[str] = []

        try:
            async with memgraph.session() as session:
                cypher = """
                MATCH (e:Entity {tenant_id: $tenant_id, kb_id: $kb_id})-[r:RELATED_TO]->(t:Entity)
                RETURN e.name as source, count(r) as degree, collect(t.name) as neighbors,
                       collect(r.doc_id) as doc_ids, collect(r.generation) as generations
                ORDER BY degree DESC
                LIMIT $limit;
                """
                res = await session.run(cypher, tenant_id=tenant_id, kb_id=kb_id, limit=limit)
                records = await res.data()

                for rec in records:
                    src = rec.get("source")
                    degree = rec.get("degree", 0)
                    neighbors = rec.get("neighbors", [])
                    doc_ids = rec.get("doc_ids", [])
                    generations = rec.get("generations", [])
                    if active_generations:
                        has_active_relation = any(
                            doc_id is None
                            or doc_id not in active_generations
                            or generation in {None, active_generations[doc_id]}
                            for doc_id, generation in zip(doc_ids, generations)
                        )
                        if doc_ids and not has_active_relation:
                            continue
                    if src:
                        summary = f"【核心枢纽实体 {src}】(连接度: {degree}): 关联了 {', '.join(neighbors[:5])}"
                        community_summaries.append(summary)
        except Exception as exc:
            logger.warning("Global graph retrieval branch failed", extra={"error": str(exc)})

        return community_summaries

    @classmethod
    def reciprocal_rank_fusion(
        cls,
        dense_sources: List[SourceChunk],
        sparse_sources: Optional[List[SourceChunk]] = None,
        graph_relations: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        k: int = 60,
        w_dense: float = 1.0,
        w_sparse: float = 2.0,
        top_k: int = 8
    ) -> List[SourceChunk]:
        """
        Executes Tri-Hybrid Reciprocal Rank Fusion (RRF) across:
        1. Dense Vector semantic search
        2. Sparse Full-Text keyword search (with long phrase hit boost)
        3. Knowledge Graph entity overlap boost
        Score_RRF(d) = sum(w_i / (k + rank_i(d))) + Hit_Boost + Graph_Boost
        """
        sparse_sources = sparse_sources or []
        graph_relations = graph_relations or []
        keywords = keywords or []

        scores: Dict[str, float] = {}
        chunk_map: Dict[str, SourceChunk] = {}

        # 1. Fuse Dense Vector Results
        for rank, chunk in enumerate(dense_sources):
            key = f"{chunk.doc_id}_{chunk.chunk_index}"
            chunk_map[key] = chunk
            dense_rrf = w_dense / (k + (rank + 1))
            scores[key] = scores.get(key, 0.0) + dense_rrf

        # 2. Fuse Sparse Keyword Results (Strong factual weight + Long Phrase Boost)
        for rank, chunk in enumerate(sparse_sources):
            key = f"{chunk.doc_id}_{chunk.chunk_index}"
            if key not in chunk_map:
                chunk_map[key] = chunk
            sparse_rrf = w_sparse / (k + (rank + 1))
            
            # Substring length boost
            content = chunk.content
            hit_boost = 0.0
            for kw in keywords:
                if kw in content:
                    hit_boost += (len(kw) ** 2) * 0.1

            scores[key] = scores.get(key, 0.0) + sparse_rrf + hit_boost

        # 3. Apply Knowledge Graph Grounding Boost
        for key, chunk in chunk_map.items():
            graph_boost = 0.0
            for rel in graph_relations:
                parts = rel.split(" --[")
                if parts and parts[0] in chunk.content:
                    graph_boost += 0.03
            scores[key] = scores.get(key, 0.0) + graph_boost

        # 4. Sort and return top_k
        ranked_keys = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        fused: List[SourceChunk] = []

        for key in ranked_keys[:top_k]:
            orig = chunk_map[key]
            raw_score = scores[key]
            display_score = min(0.99, max(0.40, orig.score * 0.4 + min(0.55, raw_score * 0.1)))
            fused.append(
                SourceChunk(
                    doc_id=orig.doc_id,
                    filename=orig.filename,
                    chunk_index=orig.chunk_index,
                    content=orig.content,
                    score=round(display_score, 4),
                    page_number=orig.page_number,
                    metadata=orig.metadata
                )
            )

        return fused

    @classmethod
    def rerank_sources(
        cls,
        sources: List[SourceChunk],
        query: str,
        top_k: int,
    ) -> List[SourceChunk]:
        """Applies a deterministic lexical relevance pass after candidate fusion.

        This is deliberately named as a lexical reranker rather than claiming a
        cross-encoder model that is not actually deployed.  A model reranker
        can be plugged in behind the same contract later.
        """
        keywords = cls.extract_search_keywords(query)
        if not keywords:
            return sources[:top_k]

        ranked = []
        for source in sources:
            content = source.content.lower()
            overlap = sum(1 for keyword in keywords if keyword.lower() in content)
            phrase_bonus = 0.15 if query.strip().lower() in content else 0.0
            rerank_score = min(0.99, source.score * 0.75 + overlap / max(1, len(keywords)) * 0.2 + phrase_bonus)

            # Smart Page Pinning: if a chunk spans multiple pages, pin the exact page where the matching keywords appear
            best_page = source.page_number
            if source.content:
                parts = re.split(r"<!--\s*Page\s+(\d+)\s*-->", source.content, flags=re.IGNORECASE)
                if len(parts) >= 3:
                    max_score = 0
                    for i in range(1, len(parts), 2):
                        p_num = int(parts[i])
                        sec_text = parts[i + 1].lower()
                        page_hits = sum(len(kw) for kw in keywords if kw.lower() in sec_text and len(kw) >= 2)
                        if query.strip().lower() in sec_text:
                            page_hits += 50
                        if page_hits > max_score:
                            max_score = page_hits
                            best_page = p_num

            ranked.append((rerank_score, SourceChunk(
                doc_id=source.doc_id,
                filename=source.filename,
                chunk_index=source.chunk_index,
                content=source.content,
                score=round(rerank_score, 4),
                page_number=best_page,
                metadata=source.metadata,
            )))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [source for _, source in ranked[:top_k]]
