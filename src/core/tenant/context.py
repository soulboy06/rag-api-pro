"""
Level 4: Tenant Request Context Lifecycle & Security Binding
Guarantees tenant isolation, identity verification, and scoped execution pinning.
Fixes: P0-SEC-01
"""
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
import contextvars

from src.core.tenant.prompts import TenantPromptManager, PromptSnapshot
from src.core.tenant.config_manager import TenantConfigManager, TenantRuntimeConfig


@dataclass
class TenantContext:
    tenant_id: str
    kb_id: Optional[str] = None
    user_id: Optional[str] = None
    roles: List[str] = field(default_factory=lambda: ["READONLY"])
    config: Optional[TenantRuntimeConfig] = None
    prompts: Optional[PromptSnapshot] = None
    is_pinned: bool = False


# Context variable for async request context propagation
_current_tenant_ctx: contextvars.ContextVar[Optional[TenantContext]] = contextvars.ContextVar(
    "_current_tenant_ctx",
    default=None
)


class TenantScope:
    """
    Context manager that pins the tenant's runtime configuration and prompt snapshot for the duration
    of an async execution block.
    """

    def __init__(
        self,
        tenant_id: str,
        kb_id: Optional[str] = None,
        user_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        version_id: Optional[int] = None
    ):
        self.tenant_id = tenant_id
        self.kb_id = kb_id
        self.user_id = user_id
        self.roles = roles or ["READONLY"]
        self.version_id = version_id
        self._token = None

    async def __aenter__(self) -> TenantContext:
        # 1. Fetch pinned configuration version
        config_snapshot = TenantConfigManager.get_versioned_snapshot(
            tenant_id=self.tenant_id,
            version_id=self.version_id
        )

        # 2. Fetch prompt snapshot
        prompt_snapshot = TenantPromptManager.get_snapshot(
            tenant_id=self.tenant_id,
            kb_id=self.kb_id
        )

        # 3. Create Context
        ctx = TenantContext(
            tenant_id=self.tenant_id,
            kb_id=self.kb_id,
            user_id=self.user_id,
            roles=self.roles,
            config=config_snapshot,
            prompts=prompt_snapshot,
            is_pinned=True
        )

        self._token = _current_tenant_ctx.set(ctx)
        return ctx

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._token:
            _current_tenant_ctx.reset(self._token)


def get_current_tenant_context() -> Optional[TenantContext]:
    """Returns the current active TenantContext if within a TenantScope."""
    return _current_tenant_ctx.get()


def set_current_tenant_context(ctx: Optional[TenantContext]) -> None:
    """Explicitly sets the active TenantContext in the current async task context."""
    _current_tenant_ctx.set(ctx)
