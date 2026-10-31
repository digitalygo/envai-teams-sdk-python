"""Shared synthetic parity scenarios exercised through actual public helpers."""
import asyncio
import copy
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from negotiated_transport import negotiated_transport
from test_background import FEATURES, NATIVE_ACK, task_terminal, wrapped
from test_http import sample
from test_stream import wire

from envai import (
    AsyncBackgroundClientToolRunner,
    AsyncClientToolRunner,
    AsyncEnvaiClient,
    AsyncJwksVerifier,
    BackgroundClientToolRunner,
    ClientToolRegistry,
    ClientToolRunner,
    EnvaiClient,
    JwksVerifier,
    RequestOptions,
    RetryPolicy,
    SQLiteToolState,
    parse_event,
)
from envai._schema import OPENAPI
from envai.errors import CancelledError, DeadlineExceeded, ToolStateError

FIXTURES = Path(__file__).parent / "fixtures/parity"
FIX = json.loads((FIXTURES / "fixtures.json").read_text())
SCENARIOS = json.loads((FIXTURES / "scenarios.json").read_text())


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("case", ["REPLAY-01", "REPLAY-02", "REPLAY-03"])
async def test_shared_foreground_replay(case, async_, tmp_path):
    scenario = SCENARIOS["cases"][case]
    events = copy.deepcopy(scenario["events"])
    terminal = copy.deepcopy(FIX["turn_terminal"])
    terminal["seq"] = max(e["seq"] for e in events)+1
    if case != "REPLAY-02":
        events.append(terminal)
    effects, submissions = [], []
    registry = ClientToolRegistry()

    def effect(args, ctx):
        effects.append(ctx)
        return "ok"

    async def async_effect(args, ctx):
        return effect(args, ctx)

    registry.register(FIX["request"]["data"]["tool_name"], async_effect if async_ else effect,
                      {"type": "object"})

    def http(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(200, content=wire(events), headers={"content-type": "text/event-stream"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(202, json=SCENARIOS["message_receipt"])
        submissions.append(request)
        return httpx.Response(204)

    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", transport=negotiated_transport(http))
    with SQLiteToolState(str(tmp_path / "claims.sqlite")) as store:
        common = dict(store=store, scope="parity", chat_id=SCENARIOS["execution_chat_id"],
                      turn_id=SCENARIOS["message_receipt"]["turn_id"])
        if async_:
            async def fetch():
                return FIX["jwks"]
            async with AsyncClientToolRunner(client, registry, verifier=AsyncJwksVerifier(fetch), **common) as runner:
                async for event in client.chats.send_and_stream(common["chat_id"], "same",
                        idempotency_key="stable-key", max_reconnects=0):
                    await runner.consume(event)
                    # Deliberately give an unsafe consumer every opportunity to commit.
                    await runner.wait()
            await client.aclose()
        else:
            with ClientToolRunner(client, registry, verifier=JwksVerifier(lambda: FIX["jwks"]), **common) as runner:
                for event in client.chats.send_and_stream(common["chat_id"], "same",
                        idempotency_key="stable-key", max_reconnects=0):
                    runner.consume(event)
                    runner.wait()
            client.close()
    assert len(effects) == scenario["expected_effects"]
    assert len(submissions) == scenario["expected_submissions"]


@pytest.mark.parametrize("async_", [False, True])
async def test_real_verification_cancel_race(async_, tmp_path):
    events = SCENARIOS["cases"]["CANCEL-01"]["events"]
    effects, submissions = [], []
    registry = ClientToolRegistry()
    registry.register(FIX["request"]["data"]["tool_name"], lambda args, ctx: effects.append(1), {"type": "object"})
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True,
        transport=negotiated_transport(lambda req: submissions.append(req) or httpx.Response(204)))
    with SQLiteToolState(str(tmp_path / "race.sqlite")) as store:
        kwargs = dict(store=store, scope="test", chat_id=events[0]["chat_id"])
        if async_:
            entered, release = asyncio.Event(), asyncio.Event()
            async def fetch():
                entered.set()
                await release.wait()
                return FIX["jwks"]
            async with AsyncBackgroundClientToolRunner(client, registry, verifier=AsyncJwksVerifier(fetch), **kwargs) as runner:
                await runner.consume(parse_event(events[0]))
                task = asyncio.create_task(runner.consume(parse_event(events[1])))
                await entered.wait()
                await runner.consume(parse_event(events[2]))
                release.set()
                await task
                await runner.wait()
            await client.aclose()
        else:
            entered, release = threading.Event(), threading.Event()
            def fetch_sync():
                entered.set()
                assert release.wait(3)
                return FIX["jwks"]
            with BackgroundClientToolRunner(client, registry, verifier=JwksVerifier(fetch_sync), **kwargs) as runner:
                runner.consume(parse_event(events[0]))
                task = threading.Thread(target=runner.consume, args=(parse_event(events[1]),))
                task.start()
                assert entered.wait(3)
                runner.consume(parse_event(events[2]))
                release.set()
                task.join(3)
                assert not task.is_alive()
                runner.wait()
            client.close()
    assert effects == submissions == []


def test_task_terminal_and_owner_survive_restart(tmp_path, tool_event):
    registry = ClientToolRegistry()
    effects = []
    registry.register(tool_event.data.tool_name, lambda a, c: effects.append(c) or "ok", {"type": "object"})
    path = str(tmp_path / "state.sqlite")
    with EnvaiClient("test", native_tasks=True, transport=negotiated_transport(lambda r: httpx.Response(204))) as client:
        for phase in ("cancel", "restart", "owner", "changed-owner"):
            with SQLiteToolState(path) as store, BackgroundClientToolRunner(client, registry,
                    store=store, scope="account", chat_id=tool_event.chat_id,
                    verifier=JwksVerifier(lambda: FIX["jwks"])) as runner:
                runner.consume(parse_event(FIX["ping"]))
                if phase == "cancel":
                    runner.consume(task_terminal(tool_event))
                elif phase == "restart":
                    runner.consume(wrapped(tool_event))
                    runner.wait()
                    assert effects == []
                elif phase == "owner":
                    runner.consume(wrapped(tool_event, "task-2"))
                    runner.wait()
                else:
                    with pytest.raises(ToolStateError):
                        runner.consume(wrapped(tool_event, "task-3"))
    assert len(effects) == 1 and effects[0].task_id == "task-2"


@pytest.mark.parametrize("async_", [False, True])
async def test_wait_budget_includes_capabilities_and_cancellation(async_):
    timeouts, calls = [], []
    def http(request):
        calls.append(request)
        timeouts.append(request.extensions["timeout"]["read"])
        time.sleep(0.02)
        return httpx.Response(200, headers=NATIVE_ACK, json={"version":3, "features":FEATURES})
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True, transport=httpx.MockTransport(http))
    with pytest.raises(TimeoutError if async_ else DeadlineExceeded):
        if async_:
            await client.chats.wait_for_task("origin", "job", timeout=0.005)
        else:
            client.chats.wait_for_task("origin", "job", timeout=0.005)
    assert len(calls) == 1 and 0 < timeouts[0] <= 0.005
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        if async_:
            await client.chats.wait_for_task("origin", "job", cancel=cancel)
        else:
            client.chats.wait_for_task("origin", "job", cancel=cancel)
    assert len(calls) == 1
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_pagination_uses_retry_and_cancel(async_):
    attempts = []
    payload = sample(OPENAPI["components"]["schemas"]["CompletionCollection"])
    payload["links"]["next"] = None
    def http(request):
        attempts.append(request)
        assert request.url.params["page"] == "1"
        if len(attempts) == 1:
            raise httpx.ReadError("retry safe read")
        return httpx.Response(200, json=payload)
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", transport=negotiated_transport(http), retry=RetryPolicy(base_delay=0))
    if async_:
        values = [v async for v in client.completions.iterate("ws", "chat")]
    else:
        values = list(client.completions.iterate("ws", "chat"))
    assert len(attempts) == 2 and len(values) == len(payload["data"])
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(CancelledError):
        if async_:
            [v async for v in client.completions.iterate("ws", "chat", request_options=RequestOptions(cancel=cancelled))]
        else:
            list(client.completions.iterate("ws", "chat", request_options=RequestOptions(cancel=cancelled)))
    assert len(attempts) == 2
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("first,second", [("direct","task-1"),("task-1","direct"),("task-1","task-2")])
async def test_owner_conflicts_and_exact_restart(async_,first,second,tmp_path):
    effects, submissions = [], []
    event = parse_event(FIX["request"])
    registry = ClientToolRegistry()
    def handler(a,c):
        effects.append(c)
        return "ok"
    async def async_handler(a,c):
        return handler(a,c)
    registry.register(event.data.tool_name,async_handler if async_ else handler,{"type":"object"})
    client=(AsyncEnvaiClient if async_ else EnvaiClient)("test",native_tasks=True,transport=negotiated_transport(
        lambda req:submissions.append(req) or httpx.Response(204)))
    path=str(tmp_path/"persistent.sqlite")
    async def keys():
        return FIX["jwks"]
    try:
        for restart in (False,True):
            with SQLiteToolState(path) as store:
                kwargs=dict(store=store,scope="account",chat_id=event.chat_id)
                first_event=event if first=="direct" else wrapped(event,first)
                second_event=event if second=="direct" else wrapped(event,second)
                if async_:
                    async with AsyncBackgroundClientToolRunner(client,registry,
                            verifier=AsyncJwksVerifier(keys),**kwargs) as runner:
                        await runner.consume(parse_event(FIX["ping"]))
                        await runner.consume(first_event)
                        await runner.wait()
                        with pytest.raises(ToolStateError):
                            await runner.consume(second_event)
                else:
                    with BackgroundClientToolRunner(client,registry,
                            verifier=JwksVerifier(lambda:FIX["jwks"]),**kwargs) as runner:
                        runner.consume(parse_event(FIX["ping"]))
                        runner.consume(first_event)
                        runner.wait()
                        with pytest.raises(ToolStateError):
                            runner.consume(second_event)
        assert len(effects)==len(submissions)==1
        assert effects[0].task_id==(None if first=="direct" else first)
    finally:
        if async_:
            await client.aclose()
        else:
            client.close()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("next_link", [
    "https://api.example.test/custom/api/v1/workspaces/ws/chats/chat/completions?page=2",
    "?page=2", "https://untrusted.example/steal-credentials",
])
async def test_page_two_preserves_base_auth_without_following_links(async_,next_link):
    requests=[]
    def http(request):
        requests.append(request)
        assert request.url.host=="api.example.test"
        assert request.url.path=="/custom/api/v1/workspaces/ws/chats/chat/completions"
        assert request.headers["Authorization"]=="Bearer credential"
        page=int(request.url.params["page"])
        return httpx.Response(200,json={"data":[],"links":{"next":next_link if page==1 else None},"meta":{"last_page":2}})
    client=(AsyncEnvaiClient if async_ else EnvaiClient)("credential",
        base_url="https://api.example.test/custom/api/v1",transport=negotiated_transport(http))
    try:
        if async_:
            assert [v async for v in client.completions.iterate("ws","chat")]==[]
            await client.completions.list("ws","chat",page=2)
        else:
            assert list(client.completions.iterate("ws","chat"))==[]
            client.completions.list("ws","chat",page=2)
        assert [r.url.params["page"] for r in requests]==["1","2","2"]
    finally:
        if async_:
            await client.aclose()
        else:
            client.close()
