"""Stable errors. HTTP error details remain available without leaking them in logs."""

from __future__ import annotations

from typing import Any


class EnvaiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        type: str = "sdk_error",
        code: str | None = None,
        details: Any = None,
        request_id: str | None = None,
    ):
        self.message, self.http_status, self.type = message, http_status, type
        self.code, self.details, self.request_id = code, details, request_id
        # Server-provided messages/details can contain secrets: explicit properties only.
        super().__init__(f"{self.__class__.__name__}(status={http_status}, type={type})")


class AuthenticationError(EnvaiError):
    pass


class AccessDeniedError(EnvaiError):
    pass


class NotFoundError(EnvaiError):
    pass


class ConflictError(EnvaiError):
    pass


class ValidationError(EnvaiError):
    pass


class RateLimitError(EnvaiError):
    pass


class ProviderError(EnvaiError):
    pass


class FileError(EnvaiError):
    pass


class ServerError(EnvaiError):
    pass


class TransportError(EnvaiError):
    pass


class CancelledError(EnvaiError):
    pass


class ToolVerificationError(EnvaiError):
    pass


class ToolStateError(EnvaiError):
    pass


class ProtocolError(EnvaiError):
    def __init__(self, message: str, *, raw: Any = None):
        self.raw = raw
        super().__init__(message, type="protocol_error")


class ReplayGapError(ProtocolError):
    def __init__(self, expected: int, received: int):
        self.expected, self.received = expected, received
        super().__init__(f"Missing sequence range {expected}..{received - 1}")


def from_response(status: int, payload: Any, request_id: str | None) -> EnvaiError:
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    kind = error.get("type", "http_error")
    cls = {
        401: AuthenticationError,
        403: AccessDeniedError,
        404: NotFoundError,
        409: ConflictError,
        422: ValidationError,
        429: RateLimitError,
    }.get(status) or (
        ProviderError
        if kind == "provider_error"
        else FileError
        if isinstance(kind, str) and (kind.startswith("file_") or kind == "invalid_path")
        else ServerError
        if status >= 500
        else EnvaiError
    )
    return cls(
        error.get("message", f"HTTP {status}"),
        http_status=status,
        type=kind,
        code=error.get("code"),
        details=error.get("details"),
        request_id=request_id,
    )
