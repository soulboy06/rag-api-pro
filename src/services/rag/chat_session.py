"""
Level 6: Multi-Turn Conversation Session & Query Contextualization
Manages chat session history, sliding window trimming, DB persistence in PostgreSQL, and follow-up query rewriting.
"""
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.models.db_models import ChatSession as DBChatSession, ChatMessage as DBChatMessage, utcnow
from src.core.exceptions import AuthorizationError


@dataclass
class ChatMessage:
    role: str  # "user" or "assistant" or "system"
    content: str
    sources: Optional[List[Dict[str, Any]]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ChatSessionManager:
    """
    Manages multi-turn conversation sessions and contextualizes follow-up queries.
    Provides fast in-memory cache and PostgreSQL backing storage.
    """
    _sessions: Dict[str, List[ChatMessage]] = {}
    MAX_CACHED_SESSIONS = 1000
    MAX_CACHED_MESSAGES = 20

    @classmethod
    def append_message(
        cls,
        session_id: str,
        role: str,
        content: str,
        sources: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        if session_id not in cls._sessions:
            if len(cls._sessions) >= cls.MAX_CACHED_SESSIONS:
                oldest_session = next(iter(cls._sessions))
                cls._sessions.pop(oldest_session, None)
            cls._sessions[session_id] = []
        cls._sessions[session_id].append(ChatMessage(role=role, content=content, sources=sources))
        cls._sessions[session_id] = cls._sessions[session_id][-cls.MAX_CACHED_MESSAGES:]

    @classmethod
    async def append_message_db(
        cls,
        db: AsyncSession,
        session_id: str,
        role: str,
        content: str,
        sources: Optional[List[Dict[str, Any]]] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        kb_id: Optional[str] = None
    ) -> None:
        """Persists a message after validating session ownership, then caches it."""
        stmt = select(DBChatSession).where(DBChatSession.id == session_id).with_for_update()
        db_session = (await db.execute(stmt)).scalar_one_or_none()

        if db_session and any(value is not None for value in (tenant_id, user_id, kb_id)):
            if (
                db_session.tenant_id != tenant_id
                or db_session.user_id != user_id
                or db_session.kb_id != kb_id
            ):
                raise AuthorizationError("Chat session does not belong to the current user and knowledge base")

        if not db_session and tenant_id and user_id and kb_id:
            db_session = DBChatSession(
                id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                kb_id=kb_id,
                title=content[:30] if role == "user" else "新对话",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            db.add(db_session)
            await db.flush()

        if db_session:
            db.add(DBChatMessage(
                session_id=session_id,
                role=role,
                content=content,
                sources=sources,
                created_at=utcnow(),
            ))
            db_session.updated_at = utcnow()
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        cls.append_message(session_id, role, content, sources)

    @classmethod
    def get_history(cls, session_id: str, max_turns: int = 5) -> List[ChatMessage]:
        """Returns the most recent N turns from session history."""
        if session_id not in cls._sessions:
            return []
        # Return last 2 * max_turns messages (user + assistant)
        return cls._sessions[session_id][-(max_turns * 2):]

    @classmethod
    async def load_history_from_db(
        cls,
        db: AsyncSession,
        session_id: str,
        max_turns: int = 5,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        kb_id: Optional[str] = None,
    ) -> List[ChatMessage]:
        """Loads conversation history from PostgreSQL and syncs to memory."""
        session_stmt = select(DBChatSession).where(DBChatSession.id == session_id)
        db_session = (await db.execute(session_stmt)).scalar_one_or_none()
        if db_session and any(value is not None for value in (tenant_id, user_id, kb_id)):
            if (
                db_session.tenant_id != tenant_id
                or db_session.user_id != user_id
                or db_session.kb_id != kb_id
            ):
                raise AuthorizationError("Chat session does not belong to the current user and knowledge base")

        if db_session:
            stmt = (
                select(DBChatMessage)
                .where(DBChatMessage.session_id == session_id)
                .order_by(DBChatMessage.created_at.asc())
            )
            db_messages = (await db.execute(stmt)).scalars().all()
            cls._sessions[session_id] = [
                ChatMessage(
                    role=m.role,
                    content=m.content,
                    sources=m.sources,
                    created_at=m.created_at,
                )
                for m in db_messages
            ][-cls.MAX_CACHED_MESSAGES:]
        elif tenant_id or user_id or kb_id:
            # A caller may be starting a new session.  Do not reuse a stale
            # process-local cache entry with the same client-supplied ID.
            cls._sessions.pop(session_id, None)

        return cls.get_history(session_id, max_turns=max_turns)

    @classmethod
    def contextualize_query(cls, session_id: Optional[str], current_query: str) -> str:
        """
        Rewrites a query containing pronouns or anaphora (e.g. "它的核心优势是什么？")
        by grounding it in previous conversation turns.
        """
        if not session_id or session_id not in cls._sessions:
            return current_query

        history = cls.get_history(session_id, max_turns=2)
        if not history:
            return current_query

        # Find the last user subject
        last_user_query = ""
        for msg in reversed(history):
            if msg.role == "user":
                last_user_query = msg.content
                break

        if not last_user_query:
            return current_query

        # Rule-based / Pronoun heuristic contextualization
        pronouns = ["它", "它们", "这个", "该模型", "其", "前者", "后者"]
        needs_rewrite = any(p in current_query for p in pronouns) or len(current_query) <= 8

        if needs_rewrite:
            subject = last_user_query.replace("是什么", "").replace("有哪些", "").replace("？", "").replace("?", "").strip()
            if subject:
                for p in pronouns:
                    if p in current_query:
                        return current_query.replace(p, f"[{subject}]")
                return f"关于【{subject}】，{current_query}"

        return current_query

    @classmethod
    def clear_session(cls, session_id: str) -> None:
        if session_id in cls._sessions:
            del cls._sessions[session_id]

    @classmethod
    def reset(cls) -> None:
        cls._sessions.clear()
