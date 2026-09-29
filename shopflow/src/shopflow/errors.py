"""Service error types mapped to HTTP responses."""

from __future__ import annotations


class ServiceError(Exception):
    """An error with an HTTP status and a stable error type for logs."""

    status: int = 500
    error_type: str = "InternalError"

    def __init__(self, message: str, *, status: int | None = None, error_type: str | None = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status
        if error_type is not None:
            self.error_type = error_type


class DependencyError(ServiceError):
    status = 503
    error_type = "DependencyError"


class PoolExhaustedError(ServiceError):
    status = 503
    error_type = "PoolExhaustedError"


class CacheUnavailableError(ServiceError):
    status = 503
    error_type = "CacheUnavailableError"


class BadRequestError(ServiceError):
    status = 400
    error_type = "BadRequest"


class UnauthorizedError(ServiceError):
    status = 401
    error_type = "Unauthorized"
