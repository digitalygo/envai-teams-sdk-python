"""Shared transport policy. Mutation requests are sent once, never retried."""

from __future__ import annotations

import asyncio
import math
import platform
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, TypeAdapter

from ._schema import OPERATIONS, strict_json, validate
from .errors import CancelledError, ProtocolError, TransportError, from_response


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.25
    max_delay: float = 10.0

    def __post_init__(self):
        if (
            not isinstance(self.max_retries, int)
            or self.max_retries < 0
            or not all(math.isfinite(v) and v >= 0 for v in (self.base_delay, self.max_delay))
        ):
            raise ValueError("Retry bounds must be nonnegative")


@dataclass(frozen=True)
class RequestOptions:
    timeout: float | None = None
    cancel: threading.Event | None = field(default=None, repr=False)


def normalize_url(base: str) -> tuple[str, str]:
    p = urlsplit(base)
    if (
        p.scheme not in ("http", "https")
        or not p.netloc
        or p.username
        or p.password
        or p.query
        or p.fragment
    ):
        raise ValueError("base_url must be an HTTP(S) URL without credentials/query/fragment")
    path = p.path.rstrip("/")
    if not path.endswith("/api/v1"):
        path += "/api/v1"
    return urlunsplit((p.scheme, p.netloc, path, "", "")), urlunsplit(
        (p.scheme, p.netloc, "", "", "")
    )


def delay(policy: RetryPolicy, attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            seconds = float(retry_after)
        except ValueError:
            try:
                seconds = (
                    parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                ).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = -1
        if seconds >= 0:
            return min(seconds, policy.max_delay)
    return random.uniform(0, min(policy.max_delay, policy.base_delay * 2**attempt))


def check_cancel(options: RequestOptions | None) -> None:
    if options and options.cancel and options.cancel.is_set():
        raise CancelledError("Request cancelled locally")


class TransportBase:
    def _configure(
        self,
        api_key: str,
        base_url: str,
        jwks_url: str | None,
        timeout: float,
        connect_timeout: float,
        stream_read_timeout: float,
        retry: RetryPolicy | None,
        observer: Callable[[str, dict[str, Any]], None] | None,
    ):
        self.base_url, self.origin = normalize_url(base_url)
        self.jwks_url = jwks_url or self.origin + "/.well-known/jwks.json"
        p = urlsplit(self.jwks_url)
        if (
            p.scheme not in ("http", "https")
            or not p.netloc
            or p.username
            or p.password
            or p.fragment
        ):
            raise ValueError("jwks_url must be an HTTP(S) URL without credentials or fragment")
        if not all(
            math.isfinite(v) and v > 0 for v in (timeout, connect_timeout, stream_read_timeout)
        ):
            raise ValueError("Timeouts must be positive")
        self._headers = {
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "User-Agent": "envai-python/0.1.0a1 (python/" + platform.python_version() + ")",
        }
        self.timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self.stream_timeout = httpx.Timeout(stream_read_timeout, connect=connect_timeout)
        self.retry = retry or RetryPolicy()
        self.observer = observer

    def __repr__(self):
        return f"{self.__class__.__name__}()"

    def _observe(self, name: str, **details: Any) -> None:
        if self.observer:
            try:
                self.observer(name, details)
            except Exception:
                pass  # Observability must not change request delivery semantics.

    def _prepare(
        self,
        operation: str,
        *,
        path_params: dict[str, Any],
        query: dict[str, Any],
        headers: dict[str, Any],
        body: Any = None,
        files: Any = None,
        data: dict[str, Any] | None = None,
        request_options: RequestOptions | None = None,
    ):
        check_cancel(request_options)
        op = OPERATIONS[operation]
        path = op["path"]
        for key, value in path_params.items():
            if not isinstance(value, str) or not value or value in (".", ".."):
                raise ValueError("Path parameters must be nonempty opaque strings")
            path = path.replace("{" + key + "}", quote(value, safe=""))
        url = self.jwks_url if operation == "getJwks" else self.base_url + path
        if operation == "downloadArtifact":
            query = {
                key: query.get(key) for key in ("artifact", "expires", "generation", "signature")
            }
        auth_headers = dict(self._headers)
        if not op["auth"] or operation == "getJwks":
            auth_headers.pop("Authorization", None)
        # Header params are operation-specific, never arbitrary auth overrides.
        auth_headers.update({k: str(v) for k, v in headers.items() if v is not None})
        kwargs: dict[str, Any] = {
            "headers": auth_headers,
            "params": {
                k: ("true" if v is True else "false" if v is False else v)
                for k, v in query.items()
                if v is not None
            },
            "timeout": request_options.timeout
            if request_options and request_options.timeout is not None
            else self.timeout,
            "follow_redirects": False,
        }
        if isinstance(body, BaseModel):
            body = body.model_dump(mode="json", by_alias=True, exclude_unset=True)
        if body is not None:
            validate(op["request"], body, request=True)
            kwargs["json"] = body
        elif op["request_required"] and op["media"] == "application/json":
            raise ValueError("Request body is required")
        if files is not None:
            kwargs["files"], kwargs["data"] = files, data
        return op, url, kwargs

    def _decode(self, op: dict[str, Any], response: httpx.Response) -> Any:
        if not 200 <= response.status_code < 300:
            try:
                payload = strict_json(response.content)
            except (ValueError, UnicodeError):
                payload = None
            raise from_response(response.status_code, payload, response.headers.get("x-request-id"))
        if op["response"] is None:
            return None
        if op["response_type"] == "bytes":
            return response.content
        try:
            value = strict_json(response.content)
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("Response is not valid JSON", raw=response.content) from exc
        validate(op["response"], value)
        if op["unwrap"]:
            value = value["data"]
        from ._generated import RESPONSE_TYPES

        annotation: Any = RESPONSE_TYPES[op["response_type"]]
        return TypeAdapter(annotation).validate_python(value)


class SyncTransport(TransportBase):
    _http: httpx.Client

    def _request(self, operation: str, **kwargs: Any) -> Any:
        options = kwargs.get("request_options")
        op, url, request = self._prepare(operation, **kwargs)
        for attempt in range(self.retry.max_retries + 1):
            check_cancel(options)
            try:
                response = self._http.request(op["method"], url, **request)
            except httpx.TransportError as exc:
                if op["method"] != "GET" or attempt == self.retry.max_retries:
                    raise TransportError(
                        "HTTP transport failed; a mutation may have been accepted"
                    ) from exc
                wait = delay(self.retry, attempt)
            else:
                if (
                    op["method"] != "GET"
                    or response.status_code not in (429, 502, 503, 504)
                    or attempt == self.retry.max_retries
                ):
                    check_cancel(options)
                    return self._decode(op, response)
                wait = delay(self.retry, attempt, response.headers.get("retry-after"))
            self._observe("http.retry", operation=operation, attempt=attempt + 1)
            if options and options.cancel:
                options.cancel.wait(wait)
            else:
                time.sleep(wait)
        raise AssertionError("unreachable")


class AsyncTransport(TransportBase):
    _http: httpx.AsyncClient

    async def _request(self, operation: str, **kwargs: Any) -> Any:
        options = kwargs.get("request_options")
        op, url, request = self._prepare(operation, **kwargs)
        for attempt in range(self.retry.max_retries + 1):
            check_cancel(options)
            try:
                response = await self._http.request(op["method"], url, **request)
            except httpx.TransportError as exc:
                if op["method"] != "GET" or attempt == self.retry.max_retries:
                    raise TransportError(
                        "HTTP transport failed; a mutation may have been accepted"
                    ) from exc
                wait = delay(self.retry, attempt)
            else:
                if (
                    op["method"] != "GET"
                    or response.status_code not in (429, 502, 503, 504)
                    or attempt == self.retry.max_retries
                ):
                    check_cancel(options)
                    return self._decode(op, response)
                wait = delay(self.retry, attempt, response.headers.get("retry-after"))
            self._observe("http.retry", operation=operation, attempt=attempt + 1)
            await asyncio.sleep(wait)
        raise AssertionError("unreachable")
