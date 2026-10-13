from __future__ import annotations

import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import rfc8785
from negotiated_transport import negotiated_transport

from envai import (
    AsyncClientToolRunner,
    AsyncEnvaiClient,
    AsyncJwksVerifier,
    ClientToolRegistry,
    ClientToolRunner,
    EnvaiClient,
    JwksVerifier,
    MemoryToolState,
    SQLiteToolState,
    parse_event,
)
from envai.errors import ProtocolError, ToolStateError, ToolVerificationError, TransportError
from envai.tool_verification import signing_payload, verify
from envai.tools import _output

F = Path(__file__).parent / "fixtures"
V = json.loads((F / "signatures.json").read_text())


@pytest.mark.parametrize("vector", V["vectors"], ids=lambda v: v["name"])
def test_shared_signatures(vector):
    data = {**vector, "args": json.loads(vector["args_json"])}
    assert rfc8785.dumps(data["args"]).decode() == vector["canonical"]
    payload = signing_payload(data)
    assert (
        payload.decode()
        == vector["call_id"] + "\n" + vector["tool_name"] + "\n" + vector["canonical"]
    )
    verify(V["jwks"], data, payload)
    data["args"]["tampered"] = True
    with pytest.raises(ToolVerificationError):
        verify(V["jwks"], data, signing_payload(data))


@pytest.mark.parametrize(
    "change",
    [
        {"call_id": "bad"},
        {"tool_name": "../bad"},
        {"args": []},
        {"args": {"x": float("nan")}},
        {"args": {"x": 2**1024}},
        {"args": {"x": "\ud800"}},
    ],
)
def test_canonicalization_rejected(change):
    data = {**V["vectors"][0], "args": {}, **change}
    with pytest.raises(ToolVerificationError):
        signing_payload(data)


@pytest.mark.parametrize(
    "change", [{"alg": "RS256"}, {"kid": "absent"}, {"sig": "bad="}, {"sig": "AA"}]
)
def test_invalid_signatures(change):
    data = {**V["vectors"][0], "args": {}}
    data["signature"] = {**data["signature"], **change}
    with pytest.raises(ToolVerificationError):
        verify(V["jwks"], data, signing_payload(data))


@pytest.mark.parametrize(
    "key_change",
    [
        {"kty": "RSA"},
        {"crv": "X25519"},
        {"use": "enc"},
        {"alg": "RS256"},
        {"key_ops": ["sign"]},
        {"x": "AA"},
    ],
)
def test_invalid_jwks(key_change):
    jwks = copy.deepcopy(V["jwks"])
    jwks["keys"][0].update(key_change)
    data = {**V["vectors"][0], "args": {}}
    with pytest.raises(ToolVerificationError):
        verify(jwks, data, signing_payload(data))


@pytest.mark.parametrize("async_", [False, True])
async def test_jwks_rotation_cache_and_fail_closed(async_):
    count = []

    def fetch():
        count.append(1)
        return {"keys": []} if len(count) == 1 else copy.deepcopy(V["jwks"])

    async def afetch():
        return fetch()

    verifier = AsyncJwksVerifier(afetch) if async_ else JwksVerifier(fetch)
    data = {**V["vectors"][0], "args": {}}
    for _ in range(2):
        if async_:
            await verifier.verify(data)
        else:
            verifier.verify(data)
    assert len(count) == 2
    data["args"] = {"tamper": True}
    with pytest.raises(ToolVerificationError):
        await verifier.verify(data) if async_ else verifier.verify(data)
    assert len(count) == 3
    data["signature"] = {**data["signature"], "alg": "bad"}
    with pytest.raises(ToolVerificationError):
        await verifier.verify(data) if async_ else verifier.verify(data)
    assert len(count) == 3


@pytest.mark.parametrize("factory", ["memory", "sqlite"])
def test_store_claim_atomic_persistent_and_conflict(tmp_path, factory):
    store = (
        MemoryToolState() if factory == "memory" else SQLiteToolState(str(tmp_path / "claims.db"))
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed = list(
            executor.map(
                lambda _: store.claim("account", "chat", "call", "hash", "turn"), range(50)
            )
        )
    assert sum(claimed) == 1
    assert store.claim("other-account", "chat", "call", "hash", "turn")
    with pytest.raises(ToolStateError):
        store.claim("account", "chat", "call", "different", "turn")
    store.transition("account", "chat", "call", "ambiguous")
    if factory == "sqlite":
        store.close()
        store = SQLiteToolState(str(tmp_path / "claims.db"))
    assert not store.claim("account", "chat", "call", "hash", "turn")
    assert store.get("account", "chat", "call").status == "ambiguous"
    store.resolve("account", "chat", "call", "turn")
    store.transition("account", "chat", "call", "submitted")
    assert store.get("account", "chat", "call").status == "resolved"
    store.resolve("account", "chat", "late", "turn")
    assert not store.claim("account", "chat", "late", "hash", "turn")
    with pytest.raises(ToolStateError):
        store.transition("account", "chat", "absent", "ambiguous")
    assert store.get("account", "chat", "absent") is None
    if factory == "sqlite":
        store.close()


def setup_sync(tool_event, handler=None, transport=None, schema=None, timeout=120):
    sent = []

    def http(req):
        sent.append(req)
        return httpx.Response(204)

    client = EnvaiClient("x", transport=negotiated_transport(transport or http))
    registry = ClientToolRegistry()
    if handler:
        registry.register(
            tool_event.data.tool_name, handler, schema or {"type": "object"}, timeout=timeout
        )
    store = MemoryToolState()
    runner = ClientToolRunner(
        client,
        registry,
        store=store,
        scope="deployment/account",
        chat_id=tool_event.chat_id,
        turn_id=tool_event.turn_id,
        verifier=JwksVerifier(lambda: copy.deepcopy(V["jwks"])),
    )
    return client, runner, store, sent


def test_sync_handler_once_and_resolution(tool_event, valid_events):
    calls = []
    client, runner, store, sent = setup_sync(
        tool_event, lambda args, ctx: calls.append(ctx.call_id) or {"ok": True}
    )
    runner.consume(tool_event)
    runner.wait()
    runner.consume(tool_event)
    runner.wait()
    assert calls == [tool_event.data.call_id] and len(sent) == 1
    body = json.loads(sent[0].content)
    assert body["output"] == '{"ok":true}' and not body["is_error"]
    assert store.get(runner.scope, runner.chat_id, tool_event.data.call_id).status == "submitted"
    result = next(e for e in valid_events if e["type"] == "agent.tool.result")
    runner.consume(parse_event(result))
    assert store.get(runner.scope, runner.chat_id, tool_event.data.call_id).status == "resolved"
    runner.close()
    client.close()


@pytest.mark.parametrize(
    "case",
    ["unknown", "invalid", "failed", "empty", "oversized", "nonserializable", "async-handler"],
)
def test_sync_controlled_tool_errors(tool_event, case):
    def handler(args, ctx):
        if case == "failed":
            raise RuntimeError("secret must not leak")
        return (
            ""
            if case == "empty"
            else "😀" * 131073
            if case == "oversized"
            else object()
            if case == "nonserializable"
            else "ok"
        )

    async def async_handler(args, ctx):
        return "async"

    schema = {"type": "object", "required": ["missing"]} if case == "invalid" else None
    c, r, s, sent = setup_sync(
        tool_event,
        None if case == "unknown" else async_handler if case == "async-handler" else handler,
        schema=schema,
    )
    r.consume(tool_event)
    r.wait()
    r.close()
    c.close()
    assert len(sent) == 1 and json.loads(sent[0].content)["is_error"]
    assert "secret" not in sent[0].content.decode()


def test_no_execution_on_signature_failure(tool_event):
    calls = []
    c, r, s, sent = setup_sync(tool_event, lambda a, ctx: calls.append(1))
    raw = copy.deepcopy(tool_event.raw)
    raw["data"]["args"]["tampered"] = True
    with pytest.raises(ToolVerificationError):
        r.consume(parse_event(raw))
    assert not calls and not sent
    r.close()
    c.close()


def test_ambiguous_output_never_retries(tool_event):
    calls = []
    sent = []

    def http(req):
        sent.append(req)
        raise httpx.ReadError("ambiguous")

    c, r, s, _ = setup_sync(tool_event, lambda a, ctx: calls.append(1) or "ok", transport=http)
    r.consume(tool_event)
    with pytest.raises(TransportError):
        r.wait()
    r.consume(tool_event)
    assert len(sent) == len(calls) == 1
    assert s.get(r.scope, r.chat_id, tool_event.data.call_id).status == "ambiguous"
    r.close()
    c.close()


def test_sync_deadline_cooperative(tool_event):
    started = threading.Event()

    def handler(args, ctx):
        started.set()
        ctx.cancelled.wait(1)
        ctx.check_cancelled()

    c, r, s, sent = setup_sync(tool_event, handler, timeout=0.2)
    r.consume(tool_event)
    assert started.wait(1)
    r.wait()
    assert not sent and s.get(r.scope, r.chat_id, tool_event.data.call_id).status == "ambiguous"
    r.close()
    c.close()


async def setup_async(tool_event, handler=None, schema=None, timeout=120, fetch=None, http=None):
    sent = []

    async def request(req):
        sent.append(req)
        return httpx.Response(204)

    async def jwks():
        return copy.deepcopy(V["jwks"])

    c = AsyncEnvaiClient("x", transport=negotiated_transport(http or request))
    registry = ClientToolRegistry()
    if handler:
        registry.register(
            tool_event.data.tool_name, handler, schema or {"type": "object"}, timeout=timeout
        )
    store = MemoryToolState()
    r = AsyncClientToolRunner(
        c,
        registry,
        store=store,
        scope="deployment/account",
        chat_id=tool_event.chat_id,
        turn_id=tool_event.turn_id,
        verifier=AsyncJwksVerifier(fetch or jwks),
    )
    return c, r, store, sent


async def test_async_once_and_immutable_snapshot(tool_event):
    started = asyncio.Event()
    release = asyncio.Event()
    called = []

    async def fetch():
        started.set()
        await release.wait()
        return copy.deepcopy(V["jwks"])

    async def handler(args, ctx):
        called.append(copy.deepcopy(args))
        return {"ok": True}

    c, r, s, sent = await setup_async(tool_event, handler, fetch=fetch)
    task = asyncio.create_task(r.consume(tool_event))
    await started.wait()
    tool_event.data.args["unsigned"] = "side effect"
    tool_event.raw["data"]["args"]["also_unsigned"] = True
    release.set()
    await task
    await r.wait()
    assert called == [{}] and len(sent) == 1
    await r.aclose()
    await c.aclose()


@pytest.mark.parametrize(
    "case", ["unknown", "invalid", "failure", "timeout", "sync-handler", "output-ambiguous"]
)
async def test_async_errors(tool_event, case):
    called = []

    async def handler(args, ctx):
        called.append(1)
        if case == "failure":
            raise RuntimeError("secret")
        if case == "timeout":
            await asyncio.sleep(1)
        return "ok"

    async def http(req):
        raise httpx.ReadError("ambiguous")

    c, r, s, sent = await setup_async(
        tool_event,
        None
        if case == "unknown"
        else (lambda a, c: "wrong")
        if case == "sync-handler"
        else handler,
        schema={"type": "object", "required": ["missing"]} if case == "invalid" else None,
        timeout=0.01 if case == "timeout" else 120,
        http=http if case == "output-ambiguous" else None,
    )
    await r.consume(tool_event)
    if case == "output-ambiguous":
        with pytest.raises(TransportError):
            await r.wait()
        await r.consume(tool_event)
        assert len(called) == 1
    else:
        await r.wait()
        assert len(sent) == 1 and json.loads(sent[0].content)["is_error"]
    await r.aclose()
    await c.aclose()


async def test_result_cancels_handler_and_terminal(tool_event, valid_events):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(args, ctx):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    c, r, s, sent = await setup_async(tool_event, handler)
    await r.consume(tool_event)
    await started.wait()
    result = next(e for e in valid_events if e["type"] == "agent.tool.result")
    await r.consume(parse_event(result))
    await cancelled.wait()
    await r.wait()
    assert not sent and s.get(r.scope, r.chat_id, tool_event.data.call_id).status == "resolved"
    terminal = next(e for e in valid_events if e["type"] == "turn.completed")
    await r.consume(parse_event(terminal))
    assert r.closed
    await r.consume(tool_event)
    await c.aclose()


def test_registry_snapshot_no_remote_schema():
    registry = ClientToolRegistry()
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    registry.register("tool", lambda a, c: None, schema)
    schema["properties"]["x"]["type"] = "number"
    assert registry.get("tool").schema["properties"]["x"]["type"] == "string"
    registry.get("tool").schema["properties"]["x"]["type"] = "number"
    assert registry.get("tool").schema["properties"]["x"]["type"] == "string"
    with pytest.raises(ValueError):
        registry.register("tool", lambda a, c: None, schema)
    with pytest.raises(ValueError):
        registry.register("bad/name", lambda a, c: None, schema)
    with pytest.raises(ValueError):
        registry.register(
            "remote", lambda a, c: None, {"type": "object", "$ref": "https://evil.test/schema"}
        )
    with pytest.raises(ValueError):
        registry.register("array", lambda a, c: None, {"type": "array"})
    assert _output({"x": float("nan")})["is_error"]


def test_runner_binding_and_explicit_state(tool_event):
    c, r, s, sent = setup_sync(tool_event, lambda a, c: None)
    raw = copy.deepcopy(tool_event.raw)
    raw["chat_id"] = "cht_01ARZ3NDEKTSV4RRFFQ69G5FAA"
    with pytest.raises(ProtocolError):
        r.consume(parse_event(raw))
    tool_event.data.args["unsigned"] = True
    with pytest.raises(ProtocolError):
        r.consume(tool_event)
    with pytest.raises(ValueError):
        ClientToolRunner(c, ClientToolRegistry(), store=None, scope="x", chat_id="c", turn_id="t")
    r.close()
    c.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_terminal_during_jwks_cannot_start_handler(tool_event, valid_events, async_):
    terminal = parse_event(next(e for e in valid_events if e["type"] == "turn.completed"))
    called = []
    if async_:
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch():
            started.set()
            await release.wait()
            return copy.deepcopy(V["jwks"])

        async def handler(args, ctx):
            called.append(1)

        c, r, s, sent = await setup_async(tool_event, handler, fetch=fetch)
        task = asyncio.create_task(r.consume(tool_event))
        await started.wait()
        await r.consume(terminal)
        release.set()
        await task
        await r.wait()
        assert not called and not sent
        await c.aclose()
    else:
        started, release = threading.Event(), threading.Event()

        def fetch():
            started.set()
            release.wait(1)
            return copy.deepcopy(V["jwks"])

        c, r, s, sent = setup_sync(tool_event, lambda a, ctx: called.append(1))
        r.verifier = JwksVerifier(fetch)
        worker = threading.Thread(target=r.consume, args=(tool_event,))
        worker.start()
        assert started.wait(1)
        r.consume(terminal)
        release.set()
        worker.join(1)
        r.wait()
        assert not called and not sent
        c.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_verification_time_consumes_tool_deadline(tool_event, async_):
    import time

    called = []
    if async_:

        async def fetch():
            await asyncio.sleep(0.03)
            return copy.deepcopy(V["jwks"])

        async def handler(args, ctx):
            called.append(1)

        c, r, s, sent = await setup_async(tool_event, handler, timeout=0.01, fetch=fetch)
        await r.consume(tool_event)
        await r.wait()
        await r.aclose()
        await c.aclose()
    else:

        def fetch():
            time.sleep(0.03)
            return copy.deepcopy(V["jwks"])

        c, r, s, sent = setup_sync(tool_event, lambda a, c: called.append(1), timeout=0.01)
        r.verifier = JwksVerifier(fetch)
        r.consume(tool_event)
        r.wait()
        r.close()
        c.close()
    assert not called


async def test_large_numeric_wire_integer_verified_and_executed_as_binary64(tool_event):
    vector = next(v for v in V["vectors"] if v["name"] == "numbers")
    args = json.loads(vector["args_json"])
    args["numbers"][3] = 100000000000000000000  # Go encoding/json emits decimal, not 1e20.
    raw = copy.deepcopy(tool_event.raw)
    raw["data"].update(
        call_id=vector["call_id"],
        tool_name=vector["tool_name"],
        args=args,
        signature=vector["signature"],
    )
    event = parse_event(raw)
    called = []

    async def handler(args, ctx):
        called.append(args["numbers"][3])
        assert isinstance(args["numbers"][3], float)
        return "ok"

    c, r, state, sent = await setup_async(event, handler)
    await r.consume(event)
    await r.wait()
    await r.aclose()
    await c.aclose()
    assert called == [1e20] and len(sent) == 1
    assert isinstance(
        event.raw["data"]["args"]["numbers"][3], int
    )  # Caller wire snapshot unchanged.


@pytest.mark.parametrize("async_", [False, True])
async def test_runner_concurrency_is_bounded(tool_event, async_):
    other = copy.deepcopy(tool_event.raw)
    vector = V["vectors"][1]
    other["data"].update(
        call_id=vector["call_id"],
        args=json.loads(vector["args_json"]),
        signature=vector["signature"],
    )
    second = parse_event(other)
    calls = []
    if async_:
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(args, ctx):
            calls.append(ctx.call_id)
            entered.set()
            await release.wait()
            return "ok"

        c, r, store, sent = await setup_async(tool_event, handler)
        r.max_concurrency = 1
        await r.consume(tool_event)
        await entered.wait()
        await r.consume(second)
        release.set()
        await r.wait()
        await r.consume(second)
        await r.aclose()
        await c.aclose()
    else:
        entered, release = threading.Event(), threading.Event()

        def handler(args, ctx):
            calls.append(ctx.call_id)
            entered.set()
            release.wait(1)
            return "ok"

        c, r, store, sent = setup_sync(tool_event, handler)
        r.max_concurrency = 1
        r.consume(tool_event)
        assert entered.wait(1)
        r.consume(second)
        release.set()
        r.wait()
        r.consume(second)
        r.close()
        c.close()
    assert calls == [tool_event.data.call_id] and len(sent) == 2
    assert any("tool_capacity_exceeded" in req.content.decode() for req in sent)


@pytest.mark.parametrize("async_", [False, True])
async def test_default_verifier_fetches_public_origin_jwks(tool_event, async_):
    requests = []

    def http(req):
        requests.append(req)
        return (
            httpx.Response(200, json=V["jwks"])
            if req.url.path.endswith("/jwks.json")
            else httpx.Response(204)
        )

    client = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "secret", base_url="https://example.test", transport=negotiated_transport(http)
    )
    registry = ClientToolRegistry()
    if async_:

        async def handler(args, ctx):
            return "ok"
    else:

        def handler(args, ctx):
            return "ok"

    registry.register(tool_event.data.tool_name, handler, {"type": "object"})
    runner = (AsyncClientToolRunner if async_ else ClientToolRunner)(
        client,
        registry,
        store=MemoryToolState(),
        scope="account",
        chat_id=tool_event.chat_id,
        turn_id=tool_event.turn_id,
    )
    if async_:
        async with runner:
            await runner.consume(tool_event)
            await runner.wait()
        await client.aclose()
    else:
        with runner:
            runner.consume(tool_event)
            runner.wait()
        client.close()
    assert requests[0].url.path == "/.well-known/jwks.json"
    assert "authorization" not in requests[0].headers
    assert requests[1].headers["authorization"] == "Bearer secret"
