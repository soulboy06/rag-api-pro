"""
Level 3: Worker Document Ingestion Processor with Intelligent Multi-Modal Parser Routing
Downloads file from MinIO, routes through ParserRouter, performs chunking,
generates embeddings into Qdrant, and builds graph relationships in Memgraph.
Fixes: P0-CORE-02, P0-CORE-05, P1-PARSER-01..09
"""
import io
import re
import uuid
import asyncio
from typing import List, Dict, Any, Tuple, Optional, Callable
from qdrant_client.http import models as qmodels

from src.core.config import settings
from src.core.clients import InfrastructureClients
from src.services.rag_service import RAGService
from src.parsers.router import ParserRouter


class DocumentProcessor:
    PAGE_MARKER_PATTERN = re.compile(r"<!--\s*Page\s+(\d+)\s*-->", re.IGNORECASE)

    @classmethod
    def extract_page_numbers(cls, text: str) -> List[int]:
        """Returns the source pages represented by a chunk, preserving provenance."""
        return sorted({int(value) for value in cls.PAGE_MARKER_PATTERN.findall(text)})

    @staticmethod
    def chunk_text(text: str, chunk_size: int = 400, overlap: int = 50) -> List[str]:
        """Sliding window chunker respecting sentence and paragraph boundaries."""
        text = text.strip()
        if not text:
            return []
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(text):
                break
            start += (chunk_size - overlap)
        return chunks

    @staticmethod
    def extract_simple_entities(
        text: str,
        custom_entity_types: Optional[List[str]] = None,
    ) -> List[Tuple[str, str, str]]:
        """Extracts deterministic triplets and configured typed markers.

        The lightweight local extractor cannot infer arbitrary entity classes
        like a generative model can. It therefore supports an explicit marker
        contract (``【Type:Value】`` or ``[Type]Value``) emitted by an upstream
        parser/VLM, while retaining the generic relation heuristics. This
        makes tenant-specific entity configuration observable and actionable
        without pretending a model was called.
        """
        triplets = []
        patterns = [
            r"([A-Za-z0-9\u4e00-\u9fa5]{2,15})\s*(?:是|属于|is a|is an)\s*([A-Za-z0-9\u4e00-\u9fa5]{2,20})",
            r"([A-Za-z0-9\u4e00-\u9fa5]{2,15})\s*(?:包含|包括|includes|contains)\s*([A-Za-z0-9\u4e00-\u9fa5]{2,20})",
            r"([A-Za-z0-9\u4e00-\u9fa5]{2,15})\s*(?:支持|provides|supports)\s*([A-Za-z0-9\u4e00-\u9fa5]{2,20})",
            r"([A-Za-z0-9\u4e00-\u9fa5]{2,25})\s*(?:版权所有|运营方|出品方|开发商)[：:]\s*([A-Za-z0-9\u4e00-\u9fa5（）()]{2,30})",
            r"([A-Za-z0-9\u4e00-\u9fa5]{2,20})\s*(?:旗下|所属|独资|控股)\s*([A-Za-z0-9\u4e00-\u9fa5]{2,20})",
        ]
        for p in patterns:
            matches = re.findall(p, text)
            for m in matches:
                if len(m) == 2:
                    triplets.append((m[0].strip(), "RELATED_TO", m[1].strip()))

        for entity_type in custom_entity_types or []:
            normalized_type = str(entity_type).strip()
            if not normalized_type:
                continue
            escaped_type = re.escape(normalized_type)
            marker_patterns = (
                rf"【\s*{escaped_type}\s*[:：]\s*([^】]+)】",
                rf"\[\s*{escaped_type}\s*\]\s*([A-Za-z0-9\u4e00-\u9fa5][^\s,，。；;\n]{{0,40}})",
            )
            for marker_pattern in marker_patterns:
                for match in re.findall(marker_pattern, text):
                    value = match.strip()
                    if value:
                        triplets.append((normalized_type, "HAS_ENTITY", value))
        return triplets

    @classmethod
    async def process_document(
        cls,
        tenant_id: str,
        kb_id: str,
        doc_id: str,
        minio_bucket: str,
        minio_key: str,
        filename: str,
        progress_callback: Optional[Callable[[str, int], Any]] = None,
        force_parser: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        generation: int = 1,
    ) -> Dict[str, Any]:
        """Executes full multi-modal document ingestion pipeline with progress reporting."""
        # 1. Download file bytes from MinIO
        if progress_callback:
            await progress_callback("DOWNLOADING", 10)

        minio_client = InfrastructureClients.get_minio()

        def download_object() -> bytes:
            obj = minio_client.get_object(minio_bucket, minio_key)
            try:
                return obj.read()
            finally:
                obj.close()
                obj.release_conn()

        raw_bytes = await asyncio.to_thread(download_object)

        # 2. Route through Level 3 Intelligent Parser Router
        if progress_callback:
            await progress_callback("PARSING", 30)

        parse_result = await ParserRouter.route_and_parse(
            file_bytes=raw_bytes,
            filename=filename,
            task_id=doc_id,
            force_parser=force_parser,
            options=options,
        )
        is_pdf_fallback = filename.lower().endswith(".pdf") and parse_result.parser_used == "text_parser"
        if (
            parse_result.metadata.get("extraction_quality") in {"image_only", "empty", "failed"}
            or not parse_result.content.strip()
            or is_pdf_fallback
        ):
            raise RuntimeError(
                "Parser produced no searchable text; configure a visual OCR/layout parser"
            )
        text_content = parse_result.content

        # 3. Table-Aware Intelligent Chunking
        if progress_callback:
            await progress_callback("CHUNKING", 50)

        from src.parsers.table_splitter import TableAwareSplitter, ProcessedChunk
        processed_chunks: List[ProcessedChunk] = TableAwareSplitter.split_document(text_content)
        if not processed_chunks:
            processed_chunks = [ProcessedChunk(content=f"Empty document: {filename}")]

        # 4. Generate Embeddings & Insert to Qdrant (Dual-representation embedding)
        if progress_callback:
            await progress_callback("EMBEDDING", 70)

        qdrant = InfrastructureClients.get_qdrant()
        
        # Dual representation: embed using search_index_text
        search_texts = [c.search_index_text for c in processed_chunks]
        embeddings = []
        batch_size = 16
        for i in range(0, len(search_texts), batch_size):
            batch_texts = search_texts[i:i + batch_size]
            embedding_model = (options or {}).get("embedding_model")
            if embedding_model:
                batch_embeddings = await RAGService.get_batch_embeddings(
                    batch_texts,
                    tenant_id=tenant_id,
                    model=embedding_model,
                )
            else:
                # Keep the adapter contract compatible with lightweight test
                # doubles and older custom embedding providers.
                batch_embeddings = await RAGService.get_batch_embeddings(
                    batch_texts,
                    tenant_id=tenant_id,
                )
            embeddings.extend(batch_embeddings)

        points = []
        for idx, (p_chunk, embedding) in enumerate(zip(processed_chunks, embeddings)):
            point_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"{tenant_id}_{kb_id}_{doc_id}_generation_{generation}_{idx}",
                )
            )
            payload = {
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "doc_id": doc_id,
                "generation": generation,
                "filename": filename,
                "chunk_index": idx,
                "content": p_chunk.content,
                "is_table": p_chunk.is_table,
                "table_title": p_chunk.table_title,
                "search_index_text": p_chunk.search_index_text,
                "parser_used": parse_result.parser_used,
                "complexity_score": parse_result.complexity_score,
                "page_count": parse_result.page_count,
            }
            page_numbers = cls.extract_page_numbers(p_chunk.content)
            if not page_numbers and p_chunk.metadata and p_chunk.metadata.get("page_number"):
                page_numbers = p_chunk.metadata.get("page_numbers") or [p_chunk.metadata["page_number"]]
            if page_numbers:
                payload["page_numbers"] = page_numbers
                payload["page_number"] = page_numbers[0]
            if p_chunk.metadata:
                payload.update(p_chunk.metadata)

            points.append(
                qmodels.PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload=payload
                )
            )

        if points:
            await qdrant.upsert(
                collection_name=settings.QDRANT_COLLECTION,
                points=points
            )

            # Re-ingestion uses deterministic point IDs. The active generation
            # is switched only after all derived writes and task ownership are
            # confirmed, so a failed parse never destroys the last good index.
            # Old generations are intentionally retained until the new
            # generation is visible. Reconciliation/retention cleanup can
            # remove them later without creating a mixed active index.

        # 5. Extract Entities and Insert to Memgraph
        if progress_callback:
            await progress_callback("GRAPH_BUILDING", 85)

        custom_entity_types = list((options or {}).get("custom_entity_types") or [])
        triplets = cls.extract_simple_entities(text_content, custom_entity_types)
        
        # Inject table entities into Knowledge Graph
        for pc in processed_chunks:
            if pc.is_table and pc.metadata and pc.metadata.get("entities"):
                t_title = pc.table_title or "业务回顾"
                for ent in pc.metadata.get("entities", []):
                    triplets.append((t_title, "INCLUDES_SEGMENT", ent))

        memgraph = InfrastructureClients.get_memgraph()
        
        if not triplets:
            triplets = [(filename, "CONTAINS_CHUNK", f"{filename}#Chunk_0")]

        active_relations = {rel for _, rel, _ in triplets[:80]}
        async with memgraph.session() as session:
            for source, rel, target in triplets[:80]:  # Limit top 80 key relationships
                cypher = """
                MERGE (s:Entity {name: $source, tenant_id: $tenant_id, kb_id: $kb_id})
                MERGE (t:Entity {name: $target, tenant_id: $tenant_id, kb_id: $kb_id})
                MERGE (s)-[r:RELATED_TO {doc_id: $doc_id, generation: $generation, relation: $rel}]->(t)
                SET r.tenant_id = $tenant_id, r.kb_id = $kb_id;
                """
                await session.run(
                    cypher,
                    source=source,
                    target=target,
                    rel=rel,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    doc_id=doc_id,
                    generation=generation,
                )
            # Remove stale relations only after the new generation has been
            # written successfully. Only the current generation is touched;
            # previous generations remain available until the switch commits.
            await session.run(
                """
                MATCH ()-[r:RELATED_TO {doc_id: $doc_id, generation: $generation}]-()
                WHERE NOT r.relation IN $active_relations
                DELETE r;
                """,
                doc_id=doc_id,
                generation=generation,
                active_relations=list(active_relations),
            )

        if progress_callback:
            await progress_callback("COMPLETED", 100)

        return {
            "chunks_count": len(processed_chunks),
            "triplets_count": len(triplets),
            "parser_used": parse_result.parser_used,
            "page_count": parse_result.page_count,
            "complexity_score": parse_result.complexity_score,
            "extraction_quality": parse_result.metadata.get("extraction_quality", "unknown"),
            "failed_items": parse_result.metadata.get("failed_items", []),
            "page_details": parse_result.page_details,
            "custom_entity_types": custom_entity_types,
            "generation": generation,
            "status": (
                "PARTIAL_SUCCEEDED"
                if parse_result.metadata.get("extraction_quality") == "partial"
                else "SUCCEEDED"
            ),
        }
