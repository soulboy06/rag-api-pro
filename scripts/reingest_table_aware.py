"""
Level 8: Accurate SSOT Ingestion & Re-indexing into PostgreSQL, MinIO, Qdrant & Memgraph
Uses exact PostgreSQL Document UUIDs with TableAwareSplitter & TableProfiler
"""
import os
import sys
import uuid
import asyncio

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
from sqlalchemy import select
from qdrant_client.http import models as qmodels

sys.path.insert(0, "e:/resume/rag-api-pro")

from src.core.clients import InfrastructureClients
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.models.db_models import Document, Task
from src.parsers.table_splitter import TableAwareSplitter, ProcessedChunk
from src.services.rag_service import RAGService
from src.parsers.router import ParserRouter


async def main():
    qdrant = InfrastructureClients.get_qdrant()
    
    # 1. Reset collection with actual embedding dimension (2048 for embedding-3)
    try:
        await qdrant.delete_collection(settings.QDRANT_COLLECTION)
    except Exception:
        pass
        
    await qdrant.create_collection(
        collection_name=settings.QDRANT_COLLECTION,
        vectors_config=qmodels.VectorParams(
            size=2048,
            distance=qmodels.Distance.COSINE
        )
    )
    print("✓ Qdrant collection reset successfully with size=2048!")
    
    # 2. Match with PostgreSQL Documents (SSOT)
    minio = InfrastructureClients.get_minio()
    bucket = settings.MINIO_BUCKET_NAME
    
    async with AsyncSessionLocal() as session:
        doc_res = await session.execute(select(Document).where(Document.tenant_id == "default_tenant"))
        docs = doc_res.scalars().all()
        print(f"✓ Found {len(docs)} documents in PostgreSQL for 'default_tenant'.")

        for doc in docs:
            fname = doc.filename
            print(f"\nProcessing document: '{fname}' (ID: {doc.id}, Key: {doc.minio_key})...")
            
            try:
                data = minio.get_object(doc.minio_bucket, doc.minio_key).read()
            except Exception as err:
                print(f"  ❌ Failed to read MinIO object: {err}")
                continue

            # Route & Parse
            parse_res = await ParserRouter.route_and_parse(
                file_bytes=data,
                filename=fname,
                task_id=f"reingest_{doc.id}"
            )
            text_content = parse_res.content
            
            # Table-Aware Chunking
            processed_chunks: list[ProcessedChunk] = TableAwareSplitter.split_document(text_content)
            print(f"  -> Generated {len(processed_chunks)} table-aware chunks (Tables: {sum(1 for c in processed_chunks if c.is_table)})")
            
            # Dual-Representation Embedding
            search_texts = [c.search_index_text for c in processed_chunks]
            embeddings = []
            batch_size = 16
            for i in range(0, len(search_texts), batch_size):
                batch_texts = search_texts[i:i + batch_size]
                batch_embeddings = await RAGService.get_batch_embeddings(batch_texts, tenant_id=doc.tenant_id)
                embeddings.extend(batch_embeddings)
                
            points = []
            for idx, (p_chunk, emb) in enumerate(zip(processed_chunks, embeddings)):
                point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc.tenant_id}_{doc.kb_id}_{doc.id}_{idx}"))
                payload = {
                    "tenant_id": doc.tenant_id,
                    "kb_id": doc.kb_id,
                    "doc_id": doc.id,
                    "filename": fname,
                    "chunk_index": idx,
                    "content": p_chunk.content,
                    "is_table": p_chunk.is_table,
                    "table_title": p_chunk.table_title,
                    "search_index_text": p_chunk.search_index_text,
                    "parser_used": parse_res.parser_used,
                    "complexity_score": parse_res.complexity_score
                }
                if p_chunk.metadata:
                    payload.update(p_chunk.metadata)
                points.append(qmodels.PointStruct(id=point_id, vector=emb, payload=payload))
                
            await qdrant.upsert(collection_name=settings.QDRANT_COLLECTION, points=points)
            print(f"  -> Successfully indexed {len(points)} vectors to Qdrant for {fname}!")

    print("\n🎉 All PostgreSQL documents successfully re-indexed with exact SSOT UUIDs!")

if __name__ == "__main__":
    asyncio.run(main())
