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
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, TypeAdapter

from ._schema import LEGACY_OPENAPI, OPERATIONS, strict_json, validate, validate_legacy_costs
from .errors import CancelledError, CompatibilityError, ProtocolError, TransportError, from_response


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
        contract_version: int,
    ):
        if type(contract_version) is not int or contract_version not in (2, 3):
            raise ValueError("contract_version must be exactly 2 or 3")
        self._contract_version = contract_version
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

    @property
    def contract_version(self) -> int:
        return self._contract_version

    @staticmethod
    def _negotiated(operation: str) -> bool:
        op = OPERATIONS[operation]
        return (
            (op["path"].startswith("/chats/") or op["path"].startswith("/atp/v3/"))
            or operation
            in ("getCapabilities", "getCapabilitiesLegacy", "getCompletion", "listCompletions")
            or ("Wiki" in operation and operation != "getWikiWebhookJwks")
        )

    def _acknowledge(self, response: httpx.Response, version: int) -> None:
        if response.headers.get_list("X-Envai-Contract-Version") != [str(version)]:
            raise CompatibilityError("Server did not acknowledge the selected contract")

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
        op = dict(OPERATIONS[operation])
        negotiated = self._negotiated(operation)
        if self.contract_version == 2 and (
            op["method"] != "GET" or ("Wiki" in operation and operation != "getWikiWebhookJwks")
        ):
            raise CompatibilityError("Legacy contract 2 is read-only and cannot access Wiki V1")
        op["operation"] = operation
        op["contract_version"] = self.contract_version if negotiated else None
        path = op["path"]
        if operation.endswith("Legacy") and self.contract_version != 2:
            raise CompatibilityError("Legacy endpoints require explicit contract_version=2")
        if self.contract_version == 2 and operation == "getCapabilities":
            path = "/capabilities"
            op["response"] = {"$ref": "#/components/schemas/Capabilities"}
        for parameter in op["params"]:
            source = (
                path_params
                if parameter["in"] == "path"
                else query
                if parameter["in"] == "query"
                else headers
            )
            value = source.get(parameter["name"])
            if value is not None and parameter["in"] != "path":
                validate(parameter.get("schema", {}), value, request=True)
            elif value is None and parameter.get("required"):
                raise ValueError("Missing required parameter " + parameter["name"])
        for key, value in path_params.items():
            if not isinstance(value, str) or not value or value in (".", ".."):
                raise ValueError("Path parameters must be nonempty opaque strings")
            path = path.replace("{" + key + "}", quote(value, safe=""))
        url = (
            self.jwks_url
            if operation == "getJwks"
            else self.origin + "/.well-known/wiki-webhook-jwks.json"
            if operation == "getWikiWebhookJwks"
            else self.base_url + path
        )
        if operation == "downloadArtifact":
            query = {
                key: query.get(key) for key in ("artifact", "expires", "generation", "signature")
            }
        auth_headers = dict(self._headers)
        if not op["auth"] or operation == "getJwks":
            auth_headers.pop("Authorization", None)
        if negotiated:
            auth_headers["X-Envai-Contract-Version"] = str(self.contract_version)
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
        if operation == "getWikiCategoryIndex":
            from .wiki import _index_options

            _index_options(
                {"from_": query.get("from"), "to": query.get("to"), "all": query.get("all")}
            )
            op["exhaustive"] = query.get("all") is True
        if files is not None:
            if operation == "uploadWikiSource":
                from .wiki import _validate_upload

                _validate_upload(files)
            kwargs["files"], kwargs["data"] = files, data
        return op, url, kwargs

    def _decode(self, op: dict[str, Any], response: httpx.Response) -> Any:
        try:
            return self._decode_response(op, response)
        except ProtocolError as exc:
            if op.get("operation") == "getCapabilities":
                raise CompatibilityError(
                    "Server capabilities do not establish the selected contract"
                ) from exc
            raise

    def _decode_response(self, op: dict[str, Any], response: httpx.Response) -> Any:
        version = op.get("contract_version")
        if response.status_code == 426 or (
            version == 3
            and response.status_code == 404
            and (
                op.get("operation") == "getCapabilities"
                or (
                    op.get("path", "").startswith("/atp/v3/")
                    and response.headers.get_list("X-Envai-Contract-Version") != ["3"]
                )
            )
        ):
            raise CompatibilityError("Server does not support the selected contract")
        if not 200 <= response.status_code < 300:
            try:
                payload = strict_json(response.content)
            except (ValueError, UnicodeError):
                payload = None
            if (
                version is not None
                and isinstance(payload, dict)
                and isinstance(payload.get("error"), dict)
                and payload["error"].get("code")
                in ("unsupported_contract_version", "contract_upgrade_required")
            ):
                raise CompatibilityError("Server rejected the selected contract")
            raise from_response(response.status_code, payload, response.headers.get("x-request-id"))
        if version is not None:
            self._acknowledge(response, version)
        if op["response"] is None:
            return None
        if op["response_type"] == "bytes":
            return response.content
        try:
            value = strict_json(response.content)
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("Response is not valid JSON", raw=response.content) from exc
        if version == 2:
            if op.get("operation") in ("getCompletion", "listCompletions"):
                validate_legacy_costs(value)
            schema = (
                {"$ref": "#/components/schemas/Capabilities"}
                if op.get("operation") in ("getCapabilities", "getCapabilitiesLegacy")
                else op["response"]
            )
            legacy = {"components": LEGACY_OPENAPI["components"], **schema}
            if next(
                Draft202012Validator(legacy, format_checker=FormatChecker()).iter_errors(value),
                None,
            ):
                raise ProtocolError(
                    "Response is not a truthful numeric legacy representation", raw=value
                )
        else:
            if op.get("operation") == "getCapabilities":
                import copy

                from ._schema import OPENAPI

                schema = copy.deepcopy(OPENAPI["components"]["schemas"]["Capabilities"])
                schema["properties"]["features"]["items"].pop("enum", None)
                validate(schema, value)
            else:
                validate(op["response"], value)
        if op.get("operation") in ("getCapabilities", "getCapabilitiesLegacy") and version == 2:
            if type(value.get("protocol_version")) is not int or value["protocol_version"] != 2:
                raise CompatibilityError("Legacy capabilities must acknowledge protocol 2")
        if op.get("operation") == "getCapabilities" and version == 3:
            caps = value.get("data", value)
            if (
                type(caps.get("protocol_version")) is not int
                or caps["protocol_version"] != 3
                or caps.get("contract_version") != 3
                or caps.get("supported_contract_versions") != [2, 3]
                or caps.get("legacy_v2_mode") != "historical_reads_replay_only"
            ):
                raise CompatibilityError("Server capabilities do not establish contract 3")
        if op.get("operation") == "getWikiCategoryIndex":
            index = value["data"]
            if index["returned_count"] != len(index["entries"]) or (
                op.get("exhaustive")
                and (not index["is_complete"] or index["returned_count"] != index["total_count"])
            ):
                raise ProtocolError(
                    "The category index was truncated or has inconsistent counts", raw=value
                )
        if op.get("operation") in ("getCompletion", "listCompletions"):
            rows = value.get("data", value)
            for row in rows if isinstance(rows, list) else [rows]:
                snapshot = row.get("terminal_snapshot")
                if snapshot and snapshot["data"]["turn_id"] != row["id"]:
                    raise ProtocolError(
                        "Terminal snapshot belongs to a different completion", raw=value
                    )
        if op["unwrap"]:
            value = value["data"]
        from ._generated import RESPONSE_TYPES

        annotation: Any = RESPONSE_TYPES[op["response_type"]]
        if version == 2 and op.get("operation") in ("getCapabilities", "getCapabilitiesLegacy"):
            from .models import CapabilitiesLegacy

            annotation = CapabilitiesLegacy
        try:
            return TypeAdapter(annotation).validate_python(value)
        except ValueError as exc:
            raise ProtocolError(
                "Response cannot be hydrated under the selected contract", raw=value
            ) from exc


class SyncTransport(TransportBase):
    _http: httpx.Client

    def _request(self, operation: str, **kwargs: Any) -> Any:
        options = kwargs.get("request_options")
        op, url, request = self._prepare(operation, **kwargs)
        if op["method"] != "GET" and self._negotiated(operation):
            self._request(
                "getCapabilities", path_params={}, query={}, headers={}, request_options=options
            )
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
        if op["method"] != "GET" and self._negotiated(operation):
            await self._request(
                "getCapabilities", path_params={}, query={}, headers={}, request_options=options
            )
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
