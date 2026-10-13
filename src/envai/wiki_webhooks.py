"""Exact-byte outgoing Wiki Ed25519 verification (not incoming provider HMAC)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter

from . import models
from ._schema import load, strict_json
from .errors import WebhookVerificationError

WIKI_SCHEMA = load("wiki.json")
VALIDATOR = Draft202012Validator(
    {**WIKI_SCHEMA, "$ref": "#/$defs/WikiWebhookEvent"}, format_checker=FormatChecker()
)
Headers = Mapping[str, Any] | Sequence[tuple[str, str]]


def signing_input(kid: str, event_id: str, timestamp: str, raw_body: bytes) -> bytes:
    return f"envai-webhook-v1\n{kid}\n{event_id}\n{timestamp}\n{hashlib.sha256(raw_body).hexdigest()}".encode(
        "ascii"
    )


def _decode(value: str, size: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Expected unpadded canonical base64url")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) != size or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise ValueError("Invalid key/signature size or encoding")
    return raw


def _headers(headers: Headers) -> dict[str, str]:
    pairs = (
        headers.multi_items()
        if hasattr(headers, "multi_items")
        else headers.items()
        if isinstance(headers, Mapping)
        else headers
    )
    found: dict[str, list[str]] = {}
    for key, value in pairs:
        if key.lower().startswith("envai-webhook-"):
            found.setdefault(key.lower(), []).extend(value if isinstance(value, list) else [value])
    result = {}
    for field in ("id", "timestamp", "key-id", "signature"):
        values = found.get("envai-webhook-" + field, [])
        if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
            raise ValueError("Missing or repeated signing header")
        result[field] = values[0]
    for field in ("id", "key-id"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", result[field]):
            raise ValueError("Malformed signing identity")
    if not re.fullmatch(r"[1-9][0-9]{0,10}", result["timestamp"]):
        raise ValueError("Malformed delivery timestamp")
    return result


class _Verifier:
    def _configure(
        self,
        workspace_id: str,
        ttl: float,
        tolerance: int,
        max_body_bytes: int,
        clock: Callable[[], float],
    ):
        if (
            not workspace_id
            or not math.isfinite(ttl)
            or not 0 <= ttl <= 300
            or tolerance <= 0
            or max_body_bytes <= 0
        ):
            raise ValueError("Expected workspace and bounded positive verification limits")
        self.workspace_id, self.ttl = workspace_id, ttl
        self.tolerance, self.max_body_bytes, self.clock = tolerance, max_body_bytes, clock
        self._cached: dict[str, Any] | None = None
        self._expires = 0.0

    def _prepare(self, body: bytes, headers: Headers, method: str):
        try:
            if (
                method != "POST"
                or not isinstance(body, bytes)
                or not 0 < len(body) <= self.max_body_bytes
            ):
                raise ValueError("Expected POST and bounded exact raw bytes")
            result = _headers(headers)
            if abs(self.clock() - int(result["timestamp"])) > self.tolerance:
                raise ValueError("Delivery timestamp outside replay window")
            _decode(result["signature"], 64)
            return result
        except (ValueError, TypeError, AttributeError) as exc:
            raise WebhookVerificationError("Invalid Wiki signing headers or body") from exc

    @staticmethod
    def _known(jwks: dict[str, Any], kid: str) -> bool:
        try:
            return any(key.get("kid") == kid for key in jwks["keys"])
        except (KeyError, TypeError, AttributeError) as exc:
            raise WebhookVerificationError("Malformed Wiki JWKS") from exc

    def _verify(self, body: bytes, headers: dict[str, str], jwks: dict[str, Any]):
        try:
            keys = [key for key in jwks["keys"] if key.get("kid") == headers["key-id"]]
            if len(keys) != 1:
                raise ValueError("Key must be unique")
            key = keys[0]
            if (
                key.get("kty") != "OKP"
                or key.get("crv") != "Ed25519"
                or key.get("alg") != "EdDSA"
                or key.get("use") != "sig"
                or "verify" not in key.get("key_ops", ["verify"])
            ):
                raise ValueError("Key is not an Ed25519 signing key")
            Ed25519PublicKey.from_public_bytes(_decode(key["x"], 32)).verify(
                _decode(headers["signature"], 64),
                signing_input(headers["key-id"], headers["id"], headers["timestamp"], body),
            )
            value = strict_json(body)
            if next(VALIDATOR.iter_errors(value), None):
                raise ValueError("Invalid Wiki event")
            if value["event_id"] != headers["id"] or value["workspace_id"] != self.workspace_id:
                raise ValueError("Wrong event identity or workspace")
            return TypeAdapter(models.WikiWebhookEvent).validate_python(value)
        except (KeyError, TypeError, ValueError, AttributeError, InvalidSignature) as exc:
            raise WebhookVerificationError("Wiki webhook verification failed") from exc


class WikiWebhookVerifier(_Verifier):
    """Configure fetch only from a trusted Envai origin; never a payload URL."""

    def __init__(
        self,
        fetch: Callable[[], dict[str, Any]],
        workspace_id: str,
        *,
        ttl: float = 300,
        tolerance: int = 300,
        max_body_bytes: int = 1_048_576,
        clock: Callable[[], float] = time.time,
    ):
        self._configure(workspace_id, ttl, tolerance, max_body_bytes, clock)
        self.fetch = fetch
        self._lock = threading.Lock()

    @classmethod
    def from_client(cls, client: Any, workspace_id: str, **options: Any):
        return cls(
            lambda: client.wiki.jwks().model_dump(mode="json", exclude_unset=True),
            workspace_id,
            **options,
        )

    def verify(self, body: bytes, headers: Headers, *, method: str = "POST"):
        parsed = self._prepare(body, headers, method)
        with self._lock:
            refreshed = False
            if self._cached is None or time.monotonic() >= self._expires:
                self._cached = self.fetch()
                self._expires = time.monotonic() + self.ttl
                refreshed = True
            if not self._known(self._cached, parsed["key-id"]) and not refreshed:
                self._cached = self.fetch()
                self._expires = time.monotonic() + self.ttl
            return self._verify(body, parsed, self._cached)


class AsyncWikiWebhookVerifier(_Verifier):
    def __init__(
        self,
        fetch: Callable[[], Awaitable[dict[str, Any]]],
        workspace_id: str,
        *,
        ttl: float = 300,
        tolerance: int = 300,
        max_body_bytes: int = 1_048_576,
        clock: Callable[[], float] = time.time,
    ):
        self._configure(workspace_id, ttl, tolerance, max_body_bytes, clock)
        self.fetch = fetch
        self._lock = asyncio.Lock()

    @classmethod
    def from_client(cls, client: Any, workspace_id: str, **options: Any):
        async def fetch():
            return (await client.wiki.jwks()).model_dump(mode="json", exclude_unset=True)

        return cls(fetch, workspace_id, **options)

    async def verify(self, body: bytes, headers: Headers, *, method: str = "POST"):
        parsed = self._prepare(body, headers, method)
        async with self._lock:
            refreshed = False
            if self._cached is None or time.monotonic() >= self._expires:
                self._cached = await self.fetch()
                self._expires = time.monotonic() + self.ttl
                refreshed = True
            if not self._known(self._cached, parsed["key-id"]) and not refreshed:
                self._cached = await self.fetch()
                self._expires = time.monotonic() + self.ttl
            return self._verify(body, parsed, self._cached)
