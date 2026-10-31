from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import httpx
import pytest
from negotiated_transport import negotiated_transport

from envai import AsyncEnvaiClient, EnvaiClient, RequestOptions, RetryPolicy, models
from envai._schema import OPENAPI, OPERATIONS
from envai._transport import delay, normalize_url
from envai.errors import (
    AccessDeniedError,
    AuthenticationError,
    CancelledError,
    ConflictError,
    NotFoundError,
    ProtocolError,
    RateLimitError,
    ServerError,
    TransportError,
    ValidationError,
)


def sample(schema):
    if "$ref" in schema:
        return sample(OPENAPI["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]])
    if "attachment_id" in schema.get("properties", {}):
        return json.loads(
            (Path(__file__).parent / "fixtures/chat-attachments/ready.json").read_text()
        )["data"]
    if "is_complete" in schema.get("properties", {}):
        return json.loads(
            (Path(__file__).parent / "fixtures/wiki/category-index.json").read_text()
        )["data"]
    if "example" in schema and schema["example"] is not None:
        # Old illustrative IDs are not always valid ULIDs.
        example = schema["example"]
        if not isinstance(example, (dict, list)) and "pattern" not in schema:
            return example
    if "const" in schema:
        return schema["const"]
    if (
        "$ref" not in schema
        and schema.get("type") == "object"
        and "scope_status" in schema.get("properties", {})
    ):
        return json.loads((Path(__file__).parent / "fixtures/wiki/usage-pending.json").read_text())[
            "data"
        ]
    if "enum" in schema:
        return schema["enum"][0]
    if "allOf" in schema:
        result = sample({k: v for k, v in schema.items() if k != "allOf"})
        for part in schema["allOf"]:
            result.update(sample(part))
        return result
    for key in ("oneOf", "anyOf"):
        if key in schema:
            parent = {k: v for k, v in schema.items() if k != key}
            return sample({**parent, **schema[key][0]})
    typ = schema.get("type", "object")
    if isinstance(typ, list):
        if "null" in typ:
            return None
        typ = typ[0]
    if typ == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not required and schema.get("minProperties", 0) and properties:
            required = [next(iter(properties))]
        return {k: sample(v) for k, v in properties.items() if k in required}
    if typ == "array":
        return [sample(schema.get("items", {})) for _ in range(schema.get("minItems", 0))]
    if typ == "integer":
        return max(0, schema.get("minimum", 0))
    if typ == "number":
        return max(0.0, schema.get("minimum", 0))
    if typ == "boolean":
        return False
    if typ == "null":
        return None
    if schema.get("format") == "date-time":
        return "2026-10-12T00:00:00Z"
    if "pattern" in schema:
        pattern = re.sub(r"\[([a-z])[A-Z]\]", r"\1", schema["pattern"])
        prefix = re.search(r"\^([a-z]+_)", pattern)
        if schema["pattern"] == "^[!-~]+$":
            return "test-idempotency"
        if schema["pattern"] == "^https://":
            return "https://receiver.example.test/wiki"
        if prefix:
            return prefix[1] + "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    return "x" * max(1, schema.get("minLength", 0))


def snake(s):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def arguments(op):
    args = {}
    for p in op["params"]:
        key = re.sub(r"[^a-zA-Z0-9_]", "_", p["name"]).strip("_")
        if "-" in p["name"]:
            key = key.lower()
        if p["in"] == "path" or p.get("required"):
            args[key] = sample(p.get("schema", {"type": "string"}))
    if op["request"]:
        if op["media"] == "multipart/form-data":
            args["files"] = {"file": ("x.png", b"png", "image/png")}
        else:
            args["body"] = sample(op["request"])
    if op["path"].endswith("/index"):
        args["all"] = True
    return args


OPS = [(key, op) for key, op in OPERATIONS.items() if not op["stream"]]


@pytest.mark.parametrize("name,op", OPS, ids=[x[0] for x in OPS])
def test_full_surface_sync(name, op):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/atp/v3/capabilities"):
            from negotiated_transport import CAPABILITIES

            return httpx.Response(
                200,
                json={
                    **CAPABILITIES,
                    "features": [
                        *CAPABILITIES["features"],
                        "chat_attachments",
                        "chat_attachment_originals",
                        "detached_background", "native_deliveries",
                        "cross_team_delegation", "delegated_job_lifecycle",
                        "phase_classification",
                    ],
                },
                headers={"X-Envai-Contract-Version": "3", "X-Envai-Extensions": request.headers.get("X-Envai-Extensions", "")},
            )
        if name == "resolveChatApproval":
            return httpx.Response(
                409, json={"error": {"type": "conflict", "message": "Not implemented"}}
            )
        ack = {"X-Envai-Contract-Version": request.headers.get("X-Envai-Contract-Version", "3"), "X-Envai-Extensions": request.headers.get("X-Envai-Extensions", "")}
        if not op["response"]:
            return httpx.Response(204, headers=ack)
        if op["response_type"] == "bytes":
            return httpx.Response(200, content=b"binary", headers=ack)
        return httpx.Response(200, json=sample(op["response"]), headers=ack)

    with EnvaiClient(
        "secret",
        base_url="https://example.test/",
        transport=httpx.MockTransport(handler),
        contract_version=2 if name.endswith("Legacy") else 3,
        native_tasks=not name.endswith("Legacy"),
    ) as client:
        method = getattr(client, snake(name))
        if name == "resolveChatApproval":
            with pytest.raises(ConflictError):
                method(**arguments(op))
        else:
            method(**arguments(op))
    expected = 2 if (op["method"] != "GET" and EnvaiClient._negotiated(name)) or name == "getDetachedTask" else 1
    assert len(seen) == expected
    req = seen[-1]
    assert req.method == op["method"]
    assert bool(req.headers.get("authorization")) == op["auth"]
    if name in ("getJwks", "getWikiWebhookJwks"):
        assert req.url.path == (
            "/.well-known/jwks.json" if name == "getJwks" else "/.well-known/wiki-webhook-jwks.json"
        )
    else:
        assert req.url.path.startswith("/api/v1/")


@pytest.mark.parametrize("name,op", OPS, ids=[x[0] for x in OPS])
async def test_full_surface_async(name, op):
    async def handler(request):
        if request.url.path.endswith("/atp/v3/capabilities"):
            from negotiated_transport import CAPABILITIES

            return httpx.Response(
                200,
                json={
                    **CAPABILITIES,
                    "features": [
                        *CAPABILITIES["features"],
                        "chat_attachments",
                        "chat_attachment_originals",
                        "detached_background", "native_deliveries",
                        "cross_team_delegation", "delegated_job_lifecycle",
                        "phase_classification",
                    ],
                },
                headers={"X-Envai-Contract-Version": "3", "X-Envai-Extensions": request.headers.get("X-Envai-Extensions", "")},
            )
        if name == "resolveChatApproval":
            return httpx.Response(
                409, json={"error": {"type": "conflict", "message": "Not implemented"}}
            )
        ack = {"X-Envai-Contract-Version": request.headers.get("X-Envai-Contract-Version", "3"), "X-Envai-Extensions": request.headers.get("X-Envai-Extensions", "")}
        if not op["response"]:
            return httpx.Response(204, headers=ack)
        if op["response_type"] == "bytes":
            return httpx.Response(200, content=b"binary", headers=ack)
        return httpx.Response(200, json=sample(op["response"]), headers=ack)

    async with AsyncEnvaiClient(
        "secret",
        transport=httpx.MockTransport(handler),
        contract_version=2 if name.endswith("Legacy") else 3,
        native_tasks=not name.endswith("Legacy"),
    ) as client:
        if name == "resolveChatApproval":
            with pytest.raises(ConflictError):
                await getattr(client, snake(name))(**arguments(op))
        else:
            await getattr(client, snake(name))(**arguments(op))


def test_operation_inventory_exact():
    shared = json.loads((Path(__file__).parent / "fixtures/operations.json").read_text())
    assert len(OPERATIONS) == 76
    for op in shared:
        assert OPERATIONS[op["id"]]["method"] == op["method"]
        assert OPERATIONS[op["id"]]["path"] == (
            "/atp/v3" + op["path"]
            if op["path"].startswith("/chats/") or op["id"] == "getCapabilities"
            else op["path"]
        )
        assert OPERATIONS[op["id"]]["auth"] == op["authenticated"]
        assert hasattr(EnvaiClient, snake(op["id"]))
        assert hasattr(AsyncEnvaiClient, snake(op["id"]))


@pytest.mark.parametrize(
    "status,cls",
    [
        (401, AuthenticationError),
        (403, AccessDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, ValidationError),
        (429, RateLimitError),
        (500, ServerError),
    ],
)
def test_errors_preserved_redacted(status, cls):
    response = {
        "error": {
            "type": "some_error",
            "message": "secret-token",
            "code": "example_code",
            "details": {"x": "secret"},
        }
    }
    with EnvaiClient(
        "secret",
        transport=negotiated_transport(
            lambda req: httpx.Response(status, json=response, headers={"x-request-id": "id"})
        ),
        retry=RetryPolicy(0),
    ) as client:
        with pytest.raises(cls) as e:
            client.list_workspaces()
        assert e.value.message == "secret-token" and e.value.code == "example_code"
        assert e.value.details == {"x": "secret"} and e.value.request_id == "id"
        assert "secret" not in str(e.value) and "secret" not in repr(client)


@pytest.mark.parametrize("method", ["send_chat_message", "update_workspace", "delete_user_tool"])
def test_mutations_never_retry(method):
    seen = []

    def handler(req):
        seen.append(req)
        raise httpx.ReadError("ambiguous")

    with EnvaiClient("x", transport=negotiated_transport(handler)) as c:
        with pytest.raises(TransportError):
            if method == "send_chat_message":
                c.send_chat_message("chat", body={"message": "hello"})
            elif method == "update_workspace":
                c.update_workspace("ws", body={"name": "x"})
            else:
                c.delete_user_tool("name")
    assert len(seen) == 1


@pytest.mark.parametrize("async_", [False, True])
async def test_get_retry(async_):
    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) < 3:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json={"data": []})

    cls = AsyncEnvaiClient if async_ else EnvaiClient
    c = cls(
        "x",
        transport=negotiated_transport(handler),
        retry=RetryPolicy(base_delay=0, max_delay=0),
        observer=lambda *a: (_ for _ in ()).throw(RuntimeError()),
    )
    if async_:
        assert await c.workspaces.list() == []
        await c.aclose()
    else:
        assert c.workspaces.list() == []
        c.close()
    assert len(seen) == 3


def test_model_omission_and_explicit_never():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(422, json={"error": {"type": "validation_error", "message": "test"}})

    with EnvaiClient("x", transport=negotiated_transport(handler)) as c:
        for body in (
            models.ApiKeyCreate(name="test"),
            models.ApiKeyCreate(name="test", expires_at="never"),
        ):
            with pytest.raises(ValidationError):
                c.api_keys.create(body=body)
    assert "expires_at" not in seen[0] and seen[1]["expires_at"] == "never"


def test_config_encoding_cancel_and_validation():
    assert normalize_url("https://example.test/")[0] == "https://example.test/api/v1"
    for url in ("ftp://example.test", "https://a:b@example.test", "https://example.test/?x=1"):
        with pytest.raises(ValueError):
            normalize_url(url)
    for retry in (-1, float("nan")):
        with pytest.raises(ValueError):
            RetryPolicy(max_retries=retry)
    assert delay(RetryPolicy(), 0, "invalid") >= 0
    assert delay(RetryPolicy(), 0, "Wed, 21 Oct 2015 07:28:00 GMT") >= 0
    assert delay(RetryPolicy(), 0, "2") == 2
    seen = []
    with EnvaiClient(
        "x", transport=negotiated_transport(lambda r: seen.append(r) or httpx.Response(404))
    ) as c:
        with pytest.raises(NotFoundError):
            c.get_user_tool("slash/name ?")
        assert "slash%2Fname%20%3F" in str(seen[-1].url)
        with pytest.raises(ValueError):
            c.get_user_tool("..")
        with pytest.raises(ValueError):
            c.send_chat_message("x", body=None)
        with pytest.raises(ValidationError):
            c.send_chat_message("x", body={"message": ""})
        event = threading.Event()
        event.set()
        with pytest.raises(CancelledError):
            c.list_workspaces(request_options=RequestOptions(cancel=event))


@pytest.mark.parametrize("content", [b"bad", b'{"data":"not a list"}', b'{"data":NaN}'])
def test_bad_response(content):
    with EnvaiClient(
        "x", transport=negotiated_transport(lambda r: httpx.Response(200, content=content))
    ) as c:
        with pytest.raises(ProtocolError):
            c.list_workspaces()


def test_signed_url_exact_and_cross_origin_blocked():
    seen = []
    url = "https://example.test/api/v1/artifacts/download?artifact=a%20b&expires=1&generation=id&signature=sign"
    with EnvaiClient(
        "secret",
        base_url="https://example.test",
        transport=negotiated_transport(
            lambda r: seen.append(r) or httpx.Response(200, content=b"png")
        ),
    ) as c:
        assert c.images.download_signed_url(url) == b"png"
        assert str(seen[-1].url) == url and "authorization" not in seen[-1].headers
        for bad in (
            "https://evil.test" + url.split("example.test")[1],
            url + "#x",
            url + "&artifact=duplicate",
            url.replace("/artifacts/download", "/elsewhere"),
        ):
            with pytest.raises(ValueError):
                c.images.download_signed_url(bad)
