"""
Level 7: Role-Based Access Control (RBAC) System
Defines user roles and provides hierarchical permission checking dependencies.
Fixes: P1-API-05, P1-API-06
"""
from enum import Enum
from typing import Dict, Any, Callable
from fastapi import Depends
from src.core.exceptions import AuthorizationError


class Role(str, Enum):
    SYSTEM_ADMIN = "SYSTEM_ADMIN"
    TENANT_ADMIN = "TENANT_ADMIN"
    MEMBER = "MEMBER"
    READONLY = "READONLY"


# Hierarchical Role Levels
ROLE_HIERARCHY: Dict[str, int] = {
    Role.SYSTEM_ADMIN.value: 40,
    Role.TENANT_ADMIN.value: 30,
    Role.MEMBER.value: 20,
    Role.READONLY.value: 10,
}


def require_role(min_role: Role) -> Callable:
    """
    FastAPI dependency factory to enforce minimum RBAC role level.
    """
    min_level = ROLE_HIERARCHY.get(min_role.value, 20)

    def role_checker(current_user: Any = None) -> Any:
        user_role = getattr(current_user, "role", "READONLY")
        user_level = ROLE_HIERARCHY.get(user_role, 0)

        if user_level < min_level:
            raise AuthorizationError(
                f"Insufficient permissions: requires '{min_role.value}', current user has '{user_role}'"
            )
        return current_user

    return role_checker
