"""
Level 4: Tenant Prompt Namespace & Immutable Snapshot Engine
Prevents cross-tenant prompt contamination and protects base system templates with DB backing.
Fixes: P1-CORE-06, P1-CORE-07
"""
import copy
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, text


# ------------------------------------------------------------------------------
# 1. Base Read-Only System Prompt Templates
# ------------------------------------------------------------------------------
DEFAULT_SYSTEM_QA_PROMPT = (
    "你是一个专业的知识库问答专家。请基于以下提供的【参考上下文】回答用户的问题。\n"
    "如果参考上下文中没有足够的信息，请明确告知用户，不要捏造虚假事实。\n\n"
    "【参考上下文】:\n{context}\n\n"
    "【用户问题】:\n{question}\n\n"
    "【专业回答】:"
)

DEFAULT_ENTITY_EXTRACTION_PROMPT = (
    "请从以下文本中提取关键实体与关系三元组 (主体, 关系, 客体)。\n"
    "【待分析文本】:\n{text}\n"
)

DEFAULT_QUERY_REWRITE_PROMPT = (
    "为了在知识库中进行多路召回，请将用户的提问改写为 3 个不同视角的搜索关键词，用逗号隔开。\n"
    "【原始提问】: {question}"
)


@dataclass(frozen=True)
class PromptSnapshot:
    """Immutable snapshot of prompt templates for a single RAG execution."""
    tenant_id: str
    kb_id: Optional[str]
    qa_template: str
    entity_template: str
    query_rewrite_template: str
    custom_persona: Optional[str] = None
    custom_entities: Tuple[str, ...] = field(default_factory=tuple)

    def render_qa(self, context: str, question: str) -> str:
        """Renders the QA prompt safely."""
        rendered = self.qa_template.replace("{context}", context).replace("{question}", question)
        if self.custom_persona:
            rendered = f"【系统人设设定】: {self.custom_persona}\n\n{rendered}"
        return rendered

    def render_rewrite(self, question: str) -> str:
        return self.query_rewrite_template.replace("{question}", question)


class TenantPromptManager:
    """
    Manages tenant and knowledge-base specific prompt namespaces.
    Ensures global templates are immutable and tenants cannot pollute each other.
    Supports in-memory overlays and PostgreSQL persistence.
    """
    _tenant_overlays: Dict[str, Dict[str, Any]] = {}
    MAX_CACHED_OVERLAYS = 5000

    @classmethod
    def set_tenant_overlay(
        cls,
        tenant_id: str,
        kb_id: Optional[str] = None,
        qa_template: Optional[str] = None,
        custom_persona: Optional[str] = None,
        custom_entities: Optional[List[str]] = None,
    ) -> None:
        """Registers or updates a prompt overlay in memory."""
        key = f"{tenant_id}:{kb_id or '*'}"
        if key not in cls._tenant_overlays:
            if len(cls._tenant_overlays) >= cls.MAX_CACHED_OVERLAYS:
                oldest_key = next(iter(cls._tenant_overlays))
                cls._tenant_overlays.pop(oldest_key, None)
            cls._tenant_overlays[key] = {}

        if qa_template is not None:
            cls._tenant_overlays[key]["qa_template"] = qa_template
        if custom_persona is not None:
            cls._tenant_overlays[key]["custom_persona"] = custom_persona
        if custom_entities is not None:
            cls._tenant_overlays[key]["custom_entities"] = list(custom_entities)

    @classmethod
    async def set_tenant_overlay_db(
        cls,
        db: AsyncSession,
        tenant_id: str,
        kb_id: Optional[str] = None,
        qa_template: Optional[str] = None,
        custom_persona: Optional[str] = None,
        custom_entities: Optional[List[str]] = None,
    ) -> None:
        """Persists the overlay first, then updates the process-local cache."""
        from src.models.db_models import TenantPrompt, utcnow
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"rag-api-pro:tenant-prompt:{tenant_id}:{kb_id or '*'}"},
        )
        stmt = select(TenantPrompt).where(
            TenantPrompt.tenant_id == tenant_id,
            TenantPrompt.kb_id == kb_id,
        ).with_for_update()
        prompt_rec = (await db.execute(stmt)).scalar_one_or_none()
        if not prompt_rec:
            prompt_rec = TenantPrompt(
                tenant_id=tenant_id,
                kb_id=kb_id,
                qa_template=qa_template,
                custom_persona=custom_persona,
                custom_entities=custom_entities,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            db.add(prompt_rec)
        else:
            if qa_template is not None:
                prompt_rec.qa_template = qa_template
            if custom_persona is not None:
                prompt_rec.custom_persona = custom_persona
            if custom_entities is not None:
                prompt_rec.custom_entities = custom_entities
            prompt_rec.updated_at = utcnow()
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        cls.set_tenant_overlay(tenant_id, kb_id, qa_template, custom_persona, custom_entities)

    @classmethod
    async def load_snapshot_db(
        cls,
        db: AsyncSession,
        tenant_id: str,
        kb_id: Optional[str] = None,
    ) -> PromptSnapshot:
        """Loads tenant and KB prompt overlays from durable storage."""
        from src.models.db_models import TenantPrompt

        conditions = [TenantPrompt.tenant_id == tenant_id]
        if kb_id is not None:
            conditions.append(or_(TenantPrompt.kb_id == kb_id, TenantPrompt.kb_id.is_(None)))
        else:
            conditions.append(TenantPrompt.kb_id.is_(None))
        rows = (
            await db.execute(
                select(TenantPrompt)
                .where(*conditions)
                .order_by(TenantPrompt.kb_id.is_(None).desc())
            )
        ).scalars().all()

        # Clear only the requested namespace before rebuilding it from DB.
        # This prevents a stale process-local overlay from surviving after a
        # restart, database restore, or explicit reset.
        cls._tenant_overlays.pop(f"{tenant_id}:*", None)
        if kb_id is not None:
            cls._tenant_overlays.pop(f"{tenant_id}:{kb_id}", None)
        for row in rows:
            key = f"{tenant_id}:{row.kb_id or '*'}"
            overlay = {}
            if row.qa_template is not None:
                overlay["qa_template"] = row.qa_template
            if row.entity_template is not None:
                overlay["entity_template"] = row.entity_template
            if row.query_rewrite_template is not None:
                overlay["query_rewrite_template"] = row.query_rewrite_template
            if row.custom_persona is not None:
                overlay["custom_persona"] = row.custom_persona
            if row.custom_entities is not None:
                overlay["custom_entities"] = list(row.custom_entities)
            if key not in cls._tenant_overlays and len(cls._tenant_overlays) >= cls.MAX_CACHED_OVERLAYS:
                oldest_key = next(iter(cls._tenant_overlays))
                cls._tenant_overlays.pop(oldest_key, None)
            cls._tenant_overlays[key] = overlay
        return cls.get_snapshot(tenant_id, kb_id)

    @classmethod
    def get_snapshot(cls, tenant_id: str, kb_id: Optional[str] = None) -> PromptSnapshot:
        """
        Creates an immutable PromptSnapshot combining base templates with tenant overlays.
        Guarantees zero mutation to global templates.
        """
        # Look for KB-specific overlay, then tenant-level overlay, then defaults
        kb_key = f"{tenant_id}:{kb_id}" if kb_id else None
        tenant_key = f"{tenant_id}:*"

        overlay = {}
        if kb_key and kb_key in cls._tenant_overlays:
            overlay = cls._tenant_overlays[kb_key]
        elif tenant_key in cls._tenant_overlays:
            overlay = cls._tenant_overlays[tenant_key]

        return PromptSnapshot(
            tenant_id=tenant_id,
            kb_id=kb_id,
            qa_template=overlay.get("qa_template", DEFAULT_SYSTEM_QA_PROMPT),
            entity_template=overlay.get("entity_template", DEFAULT_ENTITY_EXTRACTION_PROMPT),
            query_rewrite_template=overlay.get("query_rewrite_template", DEFAULT_QUERY_REWRITE_PROMPT),
            custom_persona=overlay.get("custom_persona"),
            custom_entities=tuple(overlay.get("custom_entities", []))
        )

    @classmethod
    def reset_overlays(cls) -> None:
        """Resets all tenant overlays (for testing isolation)."""
        cls._tenant_overlays.clear()
