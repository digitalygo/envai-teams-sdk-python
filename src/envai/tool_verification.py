"""Fail-closed Ed25519/JWKS verification over RFC 8785 canonical bytes."""

from __future__ import annotations

import asyncio
import base64
import math
import re
import threading
import time
from typing import Any, Awaitable, Callable

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import ToolVerificationError


def normalize_arguments(value: Any) -> Any:
    """Match the server's IEEE-754 numeric domain for JCS and execution alike.

    JSON integers outside the safe exact integer range are binary64 numbers on
    the Go/JavaScript wire. Never verify rounded bytes then execute a different,
    arbitrary-precision Python integer. Strings remain strings without coercion.
    """
    if isinstance(value, dict):
        return {key: normalize_arguments(child) for key, child in value.items()}
    if isinstance(value, list):
        return [normalize_arguments(child) for child in value]
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53 - 1:
        try:
            value = float(value)
        except OverflowError as exc:
            raise ToolVerificationError("Tool argument number exceeds binary64") from exc
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolVerificationError("Tool argument number is not finite")
    return value


def signing_payload(data: dict[str, Any]) -> bytes:
    try:
        if not re.fullmatch(r"tc_[0-7][0-9a-hjkmnp-tv-zA-HJKMNP-TV-Z]{25}", data["call_id"]):
            raise ValueError("invalid call id")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,254}", data["tool_name"]):
            raise ValueError("invalid tool name")
        if not isinstance(data["args"], dict):
            raise ValueError("arguments must be an object")
        return (
            data["call_id"].encode()
            + b"\n"
            + data["tool_name"].encode()
            + b"\n"
            + rfc8785.dumps(normalize_arguments(data["args"]))
        )
    except (KeyError, TypeError, ValueError, rfc8785.CanonicalizationError) as exc:
        raise ToolVerificationError("Client tool payload cannot be canonicalized") from exc


def _decode(value: str, size: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Expected unpadded base64url")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) != size:
        raise ValueError("Invalid key or signature size")
    return raw


def verify(jwks: dict[str, Any], data: dict[str, Any], payload: bytes) -> None:
    try:
        signature = data["signature"]
        if (
            signature["alg"] != "EdDSA"
            or not isinstance(signature["kid"], str)
            or not signature["kid"]
        ):
            raise ValueError("Unsupported signature algorithm or missing key id")
        keys = [key for key in jwks["keys"] if key.get("kid") == signature["kid"]]
        if len(keys) != 1:
            raise ValueError("Signing key must be unique")
        key = keys[0]
        if (
            key.get("kty") != "OKP"
            or key.get("crv") != "Ed25519"
            or key.get("alg", "EdDSA") != "EdDSA"
            or key.get("use", "sig") != "sig"
            or "verify" not in key.get("key_ops", ["verify"])
        ):
            raise ValueError("Key is not permitted for Ed25519 verification")
        Ed25519PublicKey.from_public_bytes(_decode(key["x"], 32)).verify(
            _decode(signature["sig"], 64), payload
        )
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise ToolVerificationError("Client tool signature verification failed") from exc


class JwksVerifier:
    def __init__(self, fetch: Callable[[], dict[str, Any]], *, ttl: float = 300):
        if ttl < 0:
            raise ValueError("JWKS TTL must be nonnegative")
        self.fetch, self.ttl = fetch, ttl
        self._cached: dict[str, Any] | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def verify(self, data: dict[str, Any]) -> bytes:
        payload = signing_payload(data)
        if data.get("signature", {}).get("alg") != "EdDSA":
            raise ToolVerificationError("Unsupported client tool signature algorithm")
        with self._lock:
            if self._cached is None or time.monotonic() >= self._expires:
                self._cached = self.fetch()
                self._expires = time.monotonic() + self.ttl
            try:
                verify(self._cached, data, payload)
            except ToolVerificationError:
                self._cached = self.fetch()  # One refresh on missing kid or rotated/bad signature.
                self._expires = time.monotonic() + self.ttl
                verify(self._cached, data, payload)
        return payload


class AsyncJwksVerifier:
    def __init__(self, fetch: Callable[[], Awaitable[dict[str, Any]]], *, ttl: float = 300):
        if ttl < 0:
            raise ValueError("JWKS TTL must be nonnegative")
        self.fetch, self.ttl = fetch, ttl
        self._cached: dict[str, Any] | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def verify(self, data: dict[str, Any]) -> bytes:
        payload = signing_payload(data)
        if data.get("signature", {}).get("alg") != "EdDSA":
            raise ToolVerificationError("Unsupported client tool signature algorithm")
        async with self._lock:
            if self._cached is None or time.monotonic() >= self._expires:
                self._cached = await self.fetch()
                self._expires = time.monotonic() + self.ttl
            try:
                verify(self._cached, data, payload)
            except ToolVerificationError:
                self._cached = await self.fetch()
                self._expires = time.monotonic() + self.ttl
                verify(self._cached, data, payload)
        return payload
