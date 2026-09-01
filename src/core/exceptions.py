"""
Level 0: Domain Exceptions & Sanitized Error Code Hierarchy
Provides clean HTTP status mappings and prevents internal stack/credential leakage.
Fixes: P3-CODE-02
"""
from typing import Any, Optional, Dict
from fastapi import status


class RAGException(Exception):
    """Base domain exception for all RAG system errors."""
    def __init__(
        self,
        message: str,
        error_code: str = "INTERNAL_ERROR",
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.details = details or {}


class AuthenticationError(RAGException):
    def __init__(self, message: str = "Invalid authentication credentials", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="AUTHENTICATION_FAILED",
            status_code=status.HTTP_401_UNAUTHORIZED,
            details=details,
        )


class PermissionDeniedError(RAGException):
    def __init__(self, message: str = "Permission denied for this resource", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="PERMISSION_DENIED",
            status_code=status.HTTP_403_FORBIDDEN,
            details=details,
        )


AuthorizationError = PermissionDeniedError


class TenantNotFoundError(RAGException):
    def __init__(self, message: str = "Tenant not found", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="TENANT_NOT_FOUND",
            status_code=status.HTTP_404_NOT_FOUND,
            details=details,
        )


class ResourceNotFoundError(RAGException):
    def __init__(self, message: str = "Requested resource not found", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="RESOURCE_NOT_FOUND",
            status_code=status.HTTP_404_NOT_FOUND,
            details=details,
        )


class ValidationError(RAGException):
    def __init__(self, message: str = "Input validation failed", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="VALIDATION_ERROR",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=details,
        )


class RateLimitExceededError(RAGException):
    def __init__(self, message: str = "Rate limit exceeded. Please retry later.", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="RATE_LIMIT_EXCEEDED",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details=details,
        )


class QuotaExceededError(RAGException):
    def __init__(self, message: str = "Tenant resource or storage quota exceeded", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="QUOTA_EXCEEDED",
            status_code=status.HTTP_403_FORBIDDEN,
            details=details,
        )


class InvalidStateTransitionError(RAGException):
    def __init__(self, message: str = "Invalid task state transition", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="INVALID_STATE_TRANSITION",
            status_code=status.HTTP_409_CONFLICT,
            details=details,
        )


class StorageUnavailableError(RAGException):
    def __init__(self, message: str = "Storage infrastructure unavailable", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="STORAGE_UNAVAILABLE",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            details=details,
        )


# Backward compatibility alias
StorageError = StorageUnavailableError


class SecurityViolationError(RAGException):
    def __init__(self, message: str = "Security policy violation detected", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="SECURITY_VIOLATION",
            status_code=status.HTTP_400_BAD_REQUEST,
            details=details,
        )


class ConfigurationError(RAGException):
    def __init__(self, message: str = "System configuration error", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code="CONFIGURATION_ERROR",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )
