from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from envai import AsyncEnvaiClient, AsyncWikiWebhookVerifier, EnvaiClient, WikiWebhookVerifier
from envai.errors import WebhookVerificationError
from envai.wiki_webhooks import _decode, signing_input

F = Path(__file__).parent / "fixtures/wiki"
VECTOR = json.loads((F / "webhook-signature.json").read_text())
# Public RFC8032 example seed, never a credential.
KEY = Ed25519PrivateKey.from_private_bytes(
    bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
)
WORKSPACE = json.loads(VECTOR["raw_body"])["workspace_id"]


def signed(body=None, *, event=None, timestamp=None, kid=None):
    body = (
        body
        if body is not None
        else json.dumps(event).encode()
        if event is not None
        else VECTOR["raw_body"].encode()
    )
    headers = dict(VECTOR["headers"])
    headers["Envai-Webhook-Timestamp"] = str(timestamp or VECTOR["now"])
    headers["Envai-Webhook-Key-Id"] = kid or headers["Envai-Webhook-Key-Id"]
    if event:
        headers["Envai-Webhook-Id"] = event["event_id"]
    headers["Envai-Webhook-Signature"] = (
        base64.urlsafe_b64encode(
            KEY.sign(
                signing_input(
                    headers["Envai-Webhook-Key-Id"],
                    headers["Envai-Webhook-Id"],
                    headers["Envai-Webhook-Timestamp"],
                    body,
                )
            )
        )
        .rstrip(b"=")
        .decode()
    )
    return body, headers


def verifier(async_=False, *, fetch=None, **options):
    if async_:

        async def afetch():
            return VECTOR["jwks"]

        return AsyncWikiWebhookVerifier(
            fetch or afetch,
            options.pop("workspace_id", WORKSPACE),
            clock=lambda: VECTOR["now"],
            **options,
        )
    return WikiWebhookVerifier(
        fetch or (lambda: VECTOR["jwks"]),
        options.pop("workspace_id", WORKSPACE),
        clock=lambda: VECTOR["now"],
        **options,
    )


async def verify(v, body, headers, **kwargs):
    result = v.verify(body, headers, **kwargs)
    return await result if isinstance(v, AsyncWikiWebhookVerifier) else result


@pytest.mark.parametrize("async_", [False, True])
async def test_exact_signature_vector_and_duplicate_header_preservation(async_):
    body = VECTOR["raw_body"].encode()
    assert (
        signing_input(
            VECTOR["headers"]["Envai-Webhook-Key-Id"],
            VECTOR["headers"]["Envai-Webhook-Id"],
            str(VECTOR["now"]),
            body,
        ).decode()
        == VECTOR["signing_input"]
    )
    v = verifier(async_)
    result = await verify(v, body, httpx.Headers(VECTOR["headers"]))
    assert result.source_revision == 3 and result.availability == "raw"
    assert result.model_dump(mode="json", by_alias=True, exclude_unset=True) == json.loads(body)
    for name, value in VECTOR["headers"].items():
        duplicates = list(VECTOR["headers"].items()) + [(name.lower(), value)]
        with pytest.raises(WebhookVerificationError):
            await verify(v, body, httpx.Headers(duplicates))


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "body",
        "space",
        "missing",
        "multiple",
        "comma",
        "newline",
        "padding",
        "bad_sig",
        "old",
        "future",
        "negative",
        "leading_zero",
        "empty",
        "array_body",
        "duplicate_json",
        "wrong_workspace",
        "wrong_id",
        "method",
        "too_large",
        "wrong_type",
        "schema",
    ],
)
async def test_verification_fail_closed(async_, case):
    body, headers = signed()
    v = verifier(async_)
    kwargs = {}
    if case == "body":
        body = body.replace(b'"raw"', b'"outbox"')
    elif case == "space":
        body += b" "
    elif case == "missing":
        del headers["Envai-Webhook-Key-Id"]
    elif case == "multiple":
        headers["Envai-Webhook-Id"] = [headers["Envai-Webhook-Id"]] * 2
    elif case == "comma":
        headers["Envai-Webhook-Id"] += ",other"
    elif case == "newline":
        headers["Envai-Webhook-Key-Id"] += "\n"
    elif case == "padding":
        headers["Envai-Webhook-Signature"] += "="
    elif case == "bad_sig":
        headers["Envai-Webhook-Signature"] = "A" * 86
    elif case in ("old", "future"):
        body, headers = signed(timestamp=VECTOR["now"] + (301 if case == "future" else -301))
    elif case == "negative":
        headers["Envai-Webhook-Timestamp"] = "-1"
    elif case == "leading_zero":
        headers["Envai-Webhook-Timestamp"] = "0" + str(VECTOR["now"])
    elif case == "empty":
        headers["Envai-Webhook-Id"] = ""
    elif case == "array_body":
        body, headers = signed(b"[]")
    elif case == "duplicate_json":
        body, headers = signed(body[:-1] + b',"source_revision":4}')
    elif case == "wrong_workspace":
        v = verifier(async_, workspace_id="other")
    elif case == "wrong_id":
        headers["Envai-Webhook-Id"] = "other"
        headers["Envai-Webhook-Signature"] = (
            base64.urlsafe_b64encode(
                KEY.sign(
                    signing_input(
                        headers["Envai-Webhook-Key-Id"],
                        "other",
                        headers["Envai-Webhook-Timestamp"],
                        body,
                    )
                )
            )
            .rstrip(b"=")
            .decode()
        )
    elif case == "method":
        kwargs["method"] = "GET"
    elif case == "too_large":
        v = verifier(async_, max_body_bytes=1)
    elif case == "wrong_type":
        body = body.decode()
    elif case == "schema":
        event = json.loads(body)
        event["source_revision"] = -1
        body, headers = signed(event=event)
    with pytest.raises(WebhookVerificationError):
        await verify(v, body, headers, **kwargs)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "case", ["alg", "crv", "kty", "use", "ops", "x", "duplicate", "missing", "malformed"]
)
async def test_jwks_metadata_strict(async_, case):
    jwks = copy.deepcopy(VECTOR["jwks"])
    if case in ("alg", "crv", "kty", "use"):
        jwks["keys"][0][case] = "wrong"
    elif case == "ops":
        jwks["keys"][0]["key_ops"] = ["sign"]
    elif case == "x":
        jwks["keys"][0]["x"] += "="
    elif case == "duplicate":
        jwks["keys"] *= 2
    elif case == "missing":
        jwks["keys"] = []
    else:
        jwks = {}

    async def afetch():
        return jwks

    v = verifier(async_, fetch=afetch if async_ else lambda: jwks)
    with pytest.raises(WebhookVerificationError):
        await verify(v, *signed())


@pytest.mark.parametrize("async_", [False, True])
async def test_rotation_refresh_is_bounded_and_cache_is_reused(async_):
    jwks = copy.deepcopy(VECTOR["jwks"])
    calls = []

    def fetch():
        calls.append(1)
        return copy.deepcopy(jwks)

    async def afetch():
        return fetch()

    v = verifier(async_, fetch=afetch if async_ else fetch)
    await verify(v, *signed())
    await verify(v, *signed())
    assert len(calls) == 1
    jwks["keys"][0]["kid"] = "rotated"
    await verify(v, *signed(kid="rotated"))
    assert len(calls) == 2
    with pytest.raises(WebhookVerificationError):
        await verify(v, *signed(kid="unknown"))
    assert len(calls) == 3
    # Invalid signatures must not cause an unbounded key fetch.
    body, headers = signed(kid="rotated")
    with pytest.raises(WebhookVerificationError):
        await verify(v, body + b" ", headers)
    assert len(calls) == 3
    v._expires = 0
    await verify(v, *signed(kid="rotated"))
    assert len(calls) == 4


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("name", ["file-processed", "file-deleted", "operation-event"])
async def test_distinct_event_shapes_and_old_event_fresh_delivery(async_, name):
    event = json.loads((F / (name + ".json")).read_text())["data"]
    event["occurred_at"] = "2000-01-01T00:00:00Z"
    result = await verify(verifier(async_), *signed(event=event))
    assert result.event_type == event["event_type"]
    if name == "operation-event":
        assert result.usage_cost.totals is None
        assert result.usage_cost.priced_subtotals[0].amount == "0.01234000"
    else:
        assert result.source_revision >= 0


@pytest.mark.parametrize("async_", [False, True])
async def test_from_client_uses_only_trusted_origin_no_bearer(async_):
    requests = []

    def handler(req):
        requests.append(req)
        assert str(req.url) == "https://envai.example/.well-known/wiki-webhook-jwks.json"
        assert "authorization" not in req.headers and "x-envai-contract-version" not in req.headers
        return httpx.Response(200, json=VECTOR["jwks"])

    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "secret", base_url="https://envai.example/api/v1", transport=httpx.MockTransport(handler)
    )
    v = (AsyncWikiWebhookVerifier if async_ else WikiWebhookVerifier).from_client(
        c, WORKSPACE, clock=lambda: VECTOR["now"]
    )
    await verify(v, *signed())
    assert len(requests) == 1
    if async_:
        await c.aclose()
    else:
        c.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"workspace_id": ""},
        {"ttl": -1},
        {"ttl": 301},
        {"ttl": float("nan")},
        {"tolerance": 0},
        {"max_body_bytes": 0},
    ],
)
def test_invalid_verifier_configuration(kwargs):
    with pytest.raises(ValueError):
        verifier(**kwargs)


@pytest.mark.parametrize(
    "encoded,size", [("A", 32), ("=" * 43, 32), ("A" * 43, 64), ("A" * 42 + "B", 32)]
)
def test_noncanonical_base64(encoded, size):
    with pytest.raises(ValueError):
        _decode(encoded, size)
