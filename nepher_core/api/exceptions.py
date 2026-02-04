"""API exception classes."""

from typing import Optional


class APIError(Exception):
    """Base exception for API errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(self.message)

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code:
            parts.append(f"(status={self.status_code})")
        return " ".join(parts)


class AuthenticationError(APIError):
    """Raised when authentication fails (401/403)."""

    pass


class NotFoundError(APIError):
    """Raised when resource is not found (404)."""

    pass


class ValidationError(APIError):
    """Raised when request validation fails (400/422)."""

    pass


class RateLimitError(APIError):
    """Raised when rate limit is exceeded (429)."""

    def __init__(
        self,
        message: str,
        retry_after: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after

