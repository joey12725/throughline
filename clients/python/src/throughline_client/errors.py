"""Exceptions raised by throughline-client."""

from __future__ import annotations


class ThroughlineError(Exception):
    """Base error for all Throughline client failures."""


class APIError(ThroughlineError):
    """Non-2xx HTTP response from the Throughline server."""

    def __init__(self, status_code: int, detail: str, *, code: str | None = None):
        self.status_code = status_code
        self.detail = detail
        self.code = code
        suffix = f" (SQLSTATE {code})" if code else ""
        super().__init__(f"HTTP {status_code}: {detail}{suffix}")


class AuthError(APIError):
    """Authentication (401) or authorization / asker-scope (403) failure."""


class ValidationError(APIError):
    """Invalid SQL, disallowed identifier, or bad request (400 / 422)."""
