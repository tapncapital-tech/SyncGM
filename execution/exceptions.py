"""Custom exceptions for Contact Sync API Adapters."""
from typing import Optional


class SyncAPIError(Exception):
    """Base exception for all sync API client failures."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body

    def __str__(self) -> str:
        if self.status_code:
            return f"[{self.status_code}] {self.message}"
        return self.message


class RateLimitError(SyncAPIError):
    """Raised when an API rate limit (HTTP 429) is encountered."""
    def __init__(self, message: str, retry_after_seconds: float = 5.0, status_code: int = 429, response_body: Optional[str] = None) -> None:
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_seconds = retry_after_seconds


class TokenExpiredError(SyncAPIError):
    """Raised when an access token has expired (HTTP 401) and cannot be automatically refreshed."""
    def __init__(self, provider: str, message: str = "Access token expired", status_code: int = 401, response_body: Optional[str] = None) -> None:
        super().__init__(f"[{provider}] {message}", status_code=status_code, response_body=response_body)
        self.provider = provider


class AuthenticationError(SyncAPIError):
    """Raised on authentication or authorization failure (e.g. invalid credentials or scopes)."""
    def __init__(self, provider: str, message: str, status_code: Optional[int] = 403, response_body: Optional[str] = None) -> None:
        super().__init__(f"[{provider}] Authentication error: {message}", status_code=status_code, response_body=response_body)
        self.provider = provider


class ContactNotFoundError(SyncAPIError):
    """Raised when a specific contact resource does not exist (HTTP 404)."""
    def __init__(self, resource_id: str, provider: str, status_code: int = 404, response_body: Optional[str] = None) -> None:
        super().__init__(f"[{provider}] Contact '{resource_id}' not found.", status_code=status_code, response_body=response_body)
        self.resource_id = resource_id
        self.provider = provider
