"""Offline native lifecycle regressions, including real signed detached tools."""

import asyncio
import copy
import json
import threading
from pathlib import Path

import httpx
import pytest
from negotiated_transport import ACK, CAPABILITIES, negotiated_transport

from envai import (
    AsyncBackgroundClientToolRunner,
    AsyncEnvaiClient,
    AsyncJwksVerifier,
    BackgroundClientToolRunner,
    ClientToolRegistry,
    EnvaiClient,
    JwksVerifier,
    MemoryToolState,
    parse_event,
)
from envai.errors import CompatibilityError, ProtocolError, ToolVerificationError, TransportError

JWKS = json.loads((Path(__file__).parent / "fixtures/signatures.json").read_text())["jwks"]
NATIVE_ACK = {**ACK, "X-Envai-Extensions": "native-tasks-v1"}
FEATURES = ["detached_background", "native_deliveries", "cross_team_delegation",
            "delegated_job_lifecycle", "phase_classification", "strict_model",
            "delegation_notify_origin"]


def wrapped(tool_event, task_id="task-1"):
    raw = copy.deepcopy(tool_event.raw)
    raw.update(v=3, type="task.event", turn_id=None,
               data={"task_id": task_id, "origin_turn_id": tool_event.turn_id,
                     "event_type": tool_event.type, "event_payload": tool_event.raw["data"]})
    return parse_event(raw)


def task_terminal(tool_event, kind="cancelled"):
    raw = copy.deepcopy(tool_event.raw)
    raw.update(v=3, type="task." + kind, turn_id=None, agent=None,
               data={"task_id": "task-1", "origin_turn_id": tool_event.turn_id,
                     "status": kind, "output": "", "error": ""})
    return parse_event(raw)


def terminal(valid_events, tool_event):
    raw = copy.deepcopy(next(x.get("event", x) for x in valid_events
                             if x.get("event", x)["type"] == "turn.completed"))
    raw.update(chat_id=tool_event.chat_id, turn_id=tool_event.turn_id)
    return parse_event(raw)


def ping(tool_event):
    raw = copy.deepcopy(tool_event.raw)
    raw.update(v=3, type="ping", seq=0, turn_id=None, agent=None, data={})
    return parse_event(raw)


def test_completion_diagnostics_are_typed(tool_event):
    raw = copy.deepcopy(tool_event.raw)
    raw.update(v=3, type="agent.done", data={"status": "completed", "summary": "done",
        "duration_ms": 10, "usage": {"input_tokens": 1, "output_tokens": 1,
        "cost_usd": None, "duration_ms": 10}, "requested_model": "requested", "model": "actual"})
    assert parse_event(raw).data.requested_model == "requested"
    assert parse_event(raw).data.model == "actual"


@pytest.mark.parametrize("async_", [False, True])
async def test_wrapped_signed_tools_after_turn_and_dedup(tool_event, valid_events, async_):
    calls, sent = [], []
    registry = ClientToolRegistry()

    def handler(args, context):
        calls.append(context)
        return "ok"

    async def async_handler(args, context):
        return handler(args, context)

    registry.register(tool_event.data.tool_name, async_handler if async_ else handler,
                      {"type": "object"})
    client_cls = AsyncEnvaiClient if async_ else EnvaiClient
    client = client_cls("test", native_tasks=True, transport=negotiated_transport(
        lambda request: sent.append(request) or httpx.Response(204)))
    kwargs = dict(store=MemoryToolState(), scope="test", chat_id=tool_event.chat_id)
    if async_:
        async def fetch():
            return JWKS
        runner = AsyncBackgroundClientToolRunner(client, registry,
            verifier=AsyncJwksVerifier(fetch), **kwargs)
        await runner.consume(ping(tool_event))
        await runner.consume(terminal(valid_events, tool_event))
        await runner.consume(tool_event)  # A late foreground request is closed.
        await runner.consume(wrapped(tool_event))
        await runner.consume(wrapped(tool_event))
        await runner.wait()
        assert not runner.closed
        await runner.aclose()
        await client.aclose()
    else:
        runner = BackgroundClientToolRunner(client, registry,
            verifier=JwksVerifier(lambda: JWKS), **kwargs)
        runner.consume(ping(tool_event))
        runner.consume(terminal(valid_events, tool_event))
        runner.consume(tool_event)
        runner.consume(wrapped(tool_event))
        runner.consume(wrapped(tool_event))
        runner.wait()
        assert not runner.closed
        runner.close()
        client.close()
    assert len(calls) == len(sent) == 1
    assert calls[0].task_id == "task-1"
    assert calls[0].turn_id == tool_event.turn_id
    assert sent[0].url.path.endswith(f"/chats/{tool_event.chat_id}/tool-output")


@pytest.mark.parametrize("async_", [False, True])
async def test_wrapped_signature_failure_and_cancel_barrier(tool_event, async_):
    calls = []
    registry = ClientToolRegistry()
    registry.register(tool_event.data.tool_name, lambda args, ctx: calls.append(ctx),
                      {"type": "object"})
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True)
    kwargs = dict(store=MemoryToolState(), scope="test", chat_id=tool_event.chat_id)
    bad = wrapped(tool_event).raw
    bad["data"]["event_payload"]["args"] = {"tampered": True}
    if async_:
        async def fetch():
            return JWKS
        runner = AsyncBackgroundClientToolRunner(client, registry,
            verifier=AsyncJwksVerifier(fetch), **kwargs)
        await runner.consume(ping(tool_event))
        with pytest.raises(ToolVerificationError):
            await runner.consume(parse_event(bad))
        await runner.consume(task_terminal(tool_event))
        await runner.consume(wrapped(tool_event))
        await runner.aclose()
        await client.aclose()
    else:
        runner = BackgroundClientToolRunner(client, registry,
            verifier=JwksVerifier(lambda: JWKS), **kwargs)
        runner.consume(ping(tool_event))
        with pytest.raises(ToolVerificationError):
            runner.consume(parse_event(bad))
        runner.consume(task_terminal(tool_event))
        runner.consume(wrapped(tool_event))
        runner.wait()
        runner.close()
        client.close()
    assert calls == []


def test_running_detached_handler_survives_turn_but_task_cancel_stops(tool_event, valid_events):
    started, finish = threading.Event(), threading.Event()
    sent, contexts = [], []

    def handler(args, ctx):
        contexts.append(ctx)
        started.set()
        finish.wait(5)
        return "result"

    client = EnvaiClient("test", native_tasks=True, transport=negotiated_transport(
        lambda req: sent.append(req) or httpx.Response(204)))
    registry = ClientToolRegistry()
    registry.register(tool_event.data.tool_name, handler, {"type": "object"})
    with BackgroundClientToolRunner(client, registry, store=MemoryToolState(), scope="test",
            chat_id=tool_event.chat_id, verifier=JwksVerifier(lambda: JWKS)) as runner:
        runner.consume(ping(tool_event))
        runner.consume(wrapped(tool_event))
        assert started.wait(5)
        runner.consume(terminal(valid_events, tool_event))
        assert not contexts[0].cancelled.is_set()
        runner.consume(task_terminal(tool_event))
        assert contexts[0].cancelled.is_set()
        finish.set()
        runner.wait()
    assert not sent
    client.close()


def api_transport(responses, seen, features=FEATURES):
    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, headers=NATIVE_ACK, json={**CAPABILITIES,
                "features": features, "families": ["turn", "agent", "chat", "task", "future"]})
        response = responses.pop(0)
        return httpx.Response(response[0], headers=NATIVE_ACK, json=response[1])
    return httpx.MockTransport(handle)


def test_capability_preflight_accepts_task_family_before_message():
    seen = []
    client = EnvaiClient("test", native_tasks=True, transport=api_transport([(202, {"status": "accepted",
        "turn_id": "cmpl_01ARZ3NDEKTSV4RRFFQ69G5FAV"})], seen))
    client.chats.send_message("chat", body={"message": "hello", "strict_model": True})
    assert [r.method for r in seen] == ["GET", "POST"]
    assert json.loads(seen[-1].content)["strict_model"] is True
    client.close()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_waiter_uses_final_origin_task_not_ack(async_, status):
    seen = []
    receipt = {"task_id": "task", "target_chat_id": "target", "turn_id": "initial"}
    base = {"task_id": "task", "origin_turn_id": "origin", "status": "running"}
    final = {**base, "status": status, "result_turn_id": "synthesis", "output": "full final"}
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True, transport=api_transport(
        [(202, receipt), (200, base), (200, final)], seen))
    body = {"workspace_id": "ws", "event_id": "event", "origin_turn_id": "origin",
            "target_chat_id": "target", "prompt": "work", "notify_origin": False,
            "strict_model": True}
    if async_:
        accepted = await client.chats.delegate("origin-chat", body=body)
        result = await client.chats.wait_for_task("origin-chat", accepted.task_id,
                                                  poll_interval=0.001)
        await client.aclose()
    else:
        accepted = client.chats.delegate("origin-chat", body=body)
        result = client.chats.wait_for_task("origin-chat", accepted.task_id, poll_interval=0.001)
        client.close()
    assert result.output == "full final" and result.result_turn_id != accepted.turn_id
    assert result.status == status
    assert all("/origin-chat/" in r.url.path for r in seen if not r.url.path.endswith("capabilities"))


@pytest.mark.parametrize("feature", ["strict_model", "delegated_job_lifecycle", "delegation_notify_origin"])
def test_unsupported_feature_fails_before_mutation(feature):
    seen = []
    with EnvaiClient("test", native_tasks=True, transport=api_transport([], seen,
            [f for f in FEATURES if f != feature])) as client:
        with pytest.raises(CompatibilityError):
            client.chats.delegate("chat", body={"workspace_id": "ws", "event_id": "event",
                "origin_turn_id": "origin", "target_chat_id": "target", "prompt": "work",
                "strict_model": True, "notify_origin": False})
    assert [r.method for r in seen] == ["GET"]


def test_native_requests_cancel_and_classification_are_not_retried():
    seen = []
    with EnvaiClient("test", native_tasks=True, transport=api_transport([
            (202, {"task_id": "task", "status": "cancelled", "subtree": True}),
            (202, {"event_id": "event", "status": "queued", "duplicate": False}),
            (503, {"error": {"type": "service_unavailable", "message": "uncertain"}})], seen)) as client:
        assert client.chats.cancel_task("chat", "task", body={"subtree": True}).subtree
        assert client.chats.deliver("chat", body={"workspace_id": "ws", "event_id": "event",
            "origin_turn_id": "origin", "task_id": "task", "status": "blocked",
            "output": "blocked"}).status == "queued"
        from envai.errors import EnvaiError
        with pytest.raises(EnvaiError):
            client.chats.classify_phase("chat", body={"request_id": "once", "task_context": {},
                "phase_id": "phase", "attempt": 1, "final_output": "final"})
    assert len([r for r in seen if r.method == "POST"]) == 3


@pytest.mark.parametrize("async_", [False, True])
async def test_waiter_bounds_identity_unknown_and_timeout(async_):
    from envai.errors import EnvaiError
    responses = [(404, {"error": {"type": "not_found", "message": "unknown",
                                 "code": "task_unknown"}}),
                 (200, {"task_id": "other", "origin_turn_id": "origin", "status": "running"})]
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True,
        transport=api_transport(responses, []))
    for timeout in [0, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            if async_:
                await client.chats.wait_for_task("chat", "task", timeout=timeout)
            else:
                client.chats.wait_for_task("chat", "task", timeout=timeout)
    for error in [EnvaiError, ProtocolError]:
        with pytest.raises(error):
            if async_:
                await client.chats.wait_for_task("chat", "task")
            else:
                client.chats.wait_for_task("chat", "task")
    responses.append((200, {"task_id": "task", "origin_turn_id": "origin", "status": "running"}))
    with pytest.raises((TimeoutError, TransportError)):
        if async_:
            await client.chats.wait_for_task("chat", "task", timeout=0.01)
        else:
            client.chats.wait_for_task("chat", "task", timeout=0.01)
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("ended", [False, True])
async def test_replay_barriers_precede_tool_dispatch(tool_event, async_, ended):
    calls = []
    registry = ClientToolRegistry()

    def handler(args, ctx):
        calls.append(ctx)
        return "done"

    async def async_handler(args, ctx):
        return handler(args, ctx)

    registry.register(tool_event.data.tool_name, async_handler if async_ else handler,
                      {"type": "object"})
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True,
        transport=negotiated_transport(lambda request: httpx.Response(204)))
    kwargs = dict(store=MemoryToolState(), scope="test", chat_id=tool_event.chat_id)
    if async_:
        async def fetch():
            return JWKS
        async with AsyncBackgroundClientToolRunner(client, registry,
                verifier=AsyncJwksVerifier(fetch), **kwargs) as runner:
            await runner.consume(wrapped(tool_event))
            assert not calls
            if ended:
                await runner.consume(task_terminal(tool_event))
            await runner.consume(ping(tool_event))
            await runner.wait()
        await client.aclose()
    else:
        with BackgroundClientToolRunner(client, registry,
                verifier=JwksVerifier(lambda: JWKS), **kwargs) as runner:
            runner.consume(wrapped(tool_event))
            assert not calls
            if ended:
                runner.consume(task_terminal(tool_event))
            runner.consume(ping(tool_event))
            runner.wait()
        client.close()
    assert len(calls) == (0 if ended else 1)


def test_extension_opt_in_is_explicit_and_applies_to_stream(tool_event):
    from envai.errors import CompatibilityError
    with EnvaiClient("test") as client:
        assert "X-Envai-Extensions" not in client._headers
        with pytest.raises(CompatibilityError):
            client.chats.get_task("chat", "task")
        with pytest.raises(CompatibilityError):
            client.chats.wait_for_task("chat", "task")
        with pytest.raises(CompatibilityError):
            client.chats.send_message("chat", body={"message": "test", "strict_model": True})
        with pytest.raises(CompatibilityError):
            BackgroundClientToolRunner(client, ClientToolRegistry(), store=MemoryToolState(),
                                     scope="test", chat_id=tool_event.chat_id)
    for value in [1, "yes"]:
        with pytest.raises(ValueError):
            EnvaiClient("test", native_tasks=value)
    with pytest.raises(ValueError):
        EnvaiClient("test", native_tasks=True, contract_version=2)
    with EnvaiClient("test", native_tasks=True) as client:
        with client.chats.events(tool_event.chat_id) as stream:
            assert stream._request()["headers"]["X-Envai-Extensions"] == "native-tasks-v1"
        assert client._headers["X-Envai-Extensions"] == "native-tasks-v1"


@pytest.mark.parametrize("async_", [False, True])
async def test_final_waiter_requires_job_lifecycle(async_):
    seen = []
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True,
        transport=api_transport([], seen, ["detached_background"]))
    with pytest.raises(CompatibilityError):
        if async_:
            await client.chats.wait_for_task("chat", "task")
        else:
            client.chats.wait_for_task("chat", "task")
    assert len(seen) == 1
    if async_:
        await client.aclose()
    else:
        client.close()


def test_direct_and_wrapped_call_id_never_execute_twice(tool_event):
    calls = []
    registry = ClientToolRegistry()
    registry.register(tool_event.data.tool_name, lambda args, ctx: calls.append(ctx) or "ok",
                      {"type": "object"})
    with EnvaiClient("test", native_tasks=True, transport=negotiated_transport(
            lambda request: httpx.Response(204))) as client:
        with BackgroundClientToolRunner(client, registry, store=MemoryToolState(), scope="test",
                chat_id=tool_event.chat_id, verifier=JwksVerifier(lambda: JWKS)) as runner:
            runner.consume(ping(tool_event))
            runner.consume(tool_event)
            runner.wait()
            runner.consume(wrapped(tool_event))
            runner.wait()
    assert len(calls) == 1


async def test_background_example_keeps_target_tools_alive_after_initial_turn(
        tool_event, valid_events, tmp_path):
    import runpy

    from envai import SQLiteToolState, models

    run_job = runpy.run_path(str(Path(__file__).parents[1] / "examples/background_job.py"))["run_job"]
    submitted = asyncio.Event()
    requests = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for seq, event in enumerate([ping(tool_event), terminal(valid_events, tool_event), wrapped(tool_event)]):
                raw = {**event.raw, "seq": seq}
                yield ("data: " + json.dumps(raw) + "\n\n").encode()
                await asyncio.sleep(0)
            await asyncio.Event().wait()

    async def http(request):
        requests.append(request)
        assert request.headers["X-Envai-Extensions"] == "native-tasks-v1"
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, headers=NATIVE_ACK, json={**CAPABILITIES, "features": FEATURES})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, headers={**NATIVE_ACK, "content-type": "text/event-stream"},
                                  stream=Stream())
        if request.url.path.endswith("/delegations"):
            return httpx.Response(202, headers=NATIVE_ACK, json={"task_id": "job",
                "target_chat_id": tool_event.chat_id, "turn_id": "initial"})
        if request.url.path.endswith("/tool-output"):
            submitted.set()
            return httpx.Response(204, headers=NATIVE_ACK)
        if request.url.path.endswith("/tasks/job"):
            await submitted.wait()
            return httpx.Response(200, headers=NATIVE_ACK, json={"task_id": "job",
                "origin_turn_id": "origin", "status": "completed", "output": "final job",
                "result_turn_id": "final-synthesis"})
        raise AssertionError(request.url)

    async def tool(args, ctx):
        return "ok"

    registry = ClientToolRegistry()
    registry.register(tool_event.data.tool_name, tool, {"type": "object"})
    store = SQLiteToolState(str(tmp_path / "calls.sqlite"))
    async with AsyncEnvaiClient("test", native_tasks=True,
                               transport=httpx.MockTransport(http)) as client:
        # Use the actual fixture key without making a JWKS network request.
        from types import SimpleNamespace
        async def get_keys():
            return SimpleNamespace(model_dump=lambda **kwargs: JWKS)
        client.get_jwks = get_keys
        result = await run_job(client, registry, store, scope="test", origin_chat="origin-chat",
            request=models.TeamDelegationRequest(workspace_id="ws", event_id="event",
                origin_turn_id="origin", target_chat_id=tool_event.chat_id,
                prompt="do work", notify_origin=False), timeout=5)
    store.close()
    assert result.output == "final job" and result.result_turn_id == "final-synthesis"
    output = next(r for r in requests if r.url.path.endswith("/tool-output"))
    assert f"/chats/{tool_event.chat_id}/" in output.url.path


def test_native_message_rejects_server_ignoring_extension_before_post():
    seen = []
    with EnvaiClient("test", native_tasks=True,
            transport=api_transport([], seen, ["client_tools"])) as client:
        with pytest.raises(CompatibilityError):
            client.chats.send_message("chat", body={"message": "work"})
    assert [request.method for request in seen] == ["GET"]


async def test_async_task_cancellation_during_signature_verification(tool_event):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def fetch():
        entered.set()
        await release.wait()
        return JWKS

    async def handler(args, ctx):
        calls.append(ctx)
        return "must not run"

    registry = ClientToolRegistry()
    registry.register(tool_event.data.tool_name, handler, {"type": "object"})
    async with AsyncEnvaiClient("test", native_tasks=True) as client:
        async with AsyncBackgroundClientToolRunner(client, registry, store=MemoryToolState(),
                scope="test", chat_id=tool_event.chat_id,
                verifier=AsyncJwksVerifier(fetch)) as runner:
            await runner.consume(ping(tool_event))
            consuming = asyncio.create_task(runner.consume(wrapped(tool_event)))
            await entered.wait()
            await runner.consume(task_terminal(tool_event))
            release.set()
            await consuming
            await runner.wait()
    assert calls == []


async def test_async_active_task_cancel_never_submits_late_output(tool_event):
    started = asyncio.Event()
    sent = []

    async def handler(args, ctx):
        started.set()
        await asyncio.Event().wait()

    async def fetch():
        return JWKS

    registry = ClientToolRegistry()
    registry.register(tool_event.data.tool_name, handler, {"type": "object"})
    async with AsyncEnvaiClient("test", native_tasks=True, transport=negotiated_transport(
            lambda request: sent.append(request) or httpx.Response(204))) as client:
        async with AsyncBackgroundClientToolRunner(client, registry, store=MemoryToolState(),
                scope="test", chat_id=tool_event.chat_id,
                verifier=AsyncJwksVerifier(fetch)) as runner:
            await runner.consume(ping(tool_event))
            await runner.consume(wrapped(tool_event))
            await started.wait()
            await runner.consume(task_terminal(tool_event))
            await runner.wait()
    assert sent == []


@pytest.mark.parametrize("ack", [None, "NATIVE-TASKS-V1", "other"])
def test_missing_extension_ack_stops_capability_hydration_before_post(ack):
    seen = []

    def handle(request):
        seen.append(request)
        headers = dict(ACK)
        if ack is not None:
            headers["X-Envai-Extensions"] = ack
        return httpx.Response(200, headers=headers,
                              json={**CAPABILITIES, "features": FEATURES})

    with EnvaiClient("test", native_tasks=True, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(CompatibilityError):
            client.chats.send_message("chat", body={"message": "work"})
    assert [request.method for request in seen] == ["GET"]


def test_missing_mutation_ack_is_uncertain_and_not_retried():
    seen = []

    def handle(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, headers=NATIVE_ACK,
                                  json={**CAPABILITIES, "features": FEATURES})
        return httpx.Response(202, headers=ACK, json={"status": "accepted",
            "turn_id": "cmpl_01ARZ3NDEKTSV4RRFFQ69G5FAV"})

    with EnvaiClient("test", native_tasks=True, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(CompatibilityError):
            client.chats.send_message("chat", body={"message": "work"})
    assert [request.method for request in seen] == ["GET", "POST"]


def test_sse_requires_extension_ack_and_neutral_events_advance_cursor(tool_event):
    neutral = {**ping(tool_event).raw, "type": "chat.extension", "seq": 1}
    next_event = {**wrapped(tool_event).raw, "seq": 2}
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in [neutral, next_event])
    for header, valid in [(ACK, False), (NATIVE_ACK, True)]:
        def handle(request):
            return httpx.Response(200, headers={**header, "content-type": "text/event-stream"},
                                  content=body)
        with EnvaiClient("test", native_tasks=True,
                         transport=httpx.MockTransport(handle)) as client:
            with client.chats.events(tool_event.chat_id, max_reconnects=0) as stream:
                if valid:
                    assert next(stream).type == "chat.extension"
                    assert stream.last_seq == 1
                    assert next(stream).type == "task.event"
                    assert stream.last_seq == 2
                else:
                    with pytest.raises(CompatibilityError):
                        next(stream)
