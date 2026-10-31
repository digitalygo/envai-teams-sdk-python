"""Library-owned delegated-job recovery through real signed tool dispatch."""
import asyncio
import copy
import threading
import time

import httpx
import pytest
from negotiated_transport import CAPABILITIES
from test_background import FEATURES, NATIVE_ACK
from test_parity_runtime import FIX, SCENARIOS
from test_stream import wire

from envai import (
    AsyncEnvaiClient,
    AsyncJwksVerifier,
    ClientToolRegistry,
    EnvaiClient,
    JwksVerifier,
    RetryPolicy,
    SQLiteToolState,
    models,
)
from envai.errors import (
    EnvaiError,
    NotFoundError,
    ProtocolError,
    ToolVerificationError,
    TransportError,
)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("mode", ["fresh", "duplicate-running", "resume", "completed", "failed", "cancelled", "unknown", "submission-error", "identity", "receipt", "signature", "eof"])
async def test_delegation_lifecycle(async_, mode, tmp_path):
    submitted, stopped = threading.Event(), threading.Event()
    effects, requests = [], []
    initial = [copy.deepcopy(FIX["task_request"]), copy.deepcopy(FIX["ping"])]
    if mode == "eof":
        initial = [copy.deepcopy(FIX["ping"])]
    if mode == "signature":
        initial[0] = copy.deepcopy(FIX["task_bad_signature"])
    if mode == "fresh":
        initial = [copy.deepcopy(FIX[k]) for k in ("ping", "turn_terminal", "task_request")]
    seq = 0
    for event in initial:
        if event["type"] != "ping":
            seq += 1
            event["seq"] = seq
    class SyncStream(httpx.SyncByteStream):
        def __iter__(self):
            for event in initial:
                yield wire([event])
            if mode == "eof":
                return
            while not stopped.wait(0.002):
                yield wire([FIX["ping"]])
        def close(self):
            stopped.set()
    class AsyncStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for event in initial:
                yield wire([event])
                await asyncio.sleep(0)
            if mode == "eof":
                return
            while not stopped.is_set():
                await asyncio.sleep(0.002)
                yield wire([FIX["ping"]])
        async def aclose(self):
            stopped.set()
    def http(request):
        requests.append(request)
        assert request.headers["X-Envai-Extensions"] == "native-tasks-v1"
        path = request.url.path
        if path.endswith("/capabilities"):
            return httpx.Response(200, headers=NATIVE_ACK, json={**CAPABILITIES, "features":FEATURES})
        if path.endswith("/events"):
            return httpx.Response(200, headers={**NATIVE_ACK,"content-type":"text/event-stream"},
                                  stream=AsyncStream() if async_ else SyncStream())
        if path.endswith("/delegations"):
            assert any(r.url.path.endswith("/events") for r in requests)
            receipt = {**SCENARIOS["delegation_receipt"]}
            if mode == "receipt":
                receipt["target_chat_id"] = "other"
            return httpx.Response(202, headers=NATIVE_ACK, json=receipt)
        if path.endswith("/tool-output"):
            assert SCENARIOS["execution_chat_id"] in path
            if mode == "submission-error":
                return httpx.Response(503, headers=NATIVE_ACK, json={"error":"provider-independent failure"})
            submitted.set()
            return httpx.Response(204, headers=NATIVE_ACK)
        if "/tasks/" in path:
            assert SCENARIOS["origin_chat_id"] in path
            if mode == "unknown":
                return httpx.Response(404, headers=NATIVE_ACK, json={"error":"task_unknown"})
            state = mode if mode in ("completed","failed","cancelled") else "completed" if submitted.is_set() else "running"
            return httpx.Response(200, headers=NATIVE_ACK, json={
                "task_id": SCENARIOS["delegation_receipt"]["task_id"],
                "origin_turn_id": FIX["request"]["turn_id"], "status":state,
                "target_chat_id": "other" if mode == "identity" else SCENARIOS["execution_chat_id"],
                "result_turn_id":"final-synthesis", "output":"final job output", "error":"job reason"})
        raise AssertionError(path)
    registry = ClientToolRegistry()
    def handler(args, ctx):
        effects.append(ctx)
        return "ok"
    async def async_handler(args,ctx):
        return handler(args,ctx)
    registry.register(FIX["request"]["data"]["tool_name"], async_handler if async_ else handler, {"type":"object"})
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True, transport=httpx.MockTransport(http), retry=RetryPolicy(base_delay=0))
    request = models.TeamDelegationRequest(workspace_id="workspace", event_id="stable-id",
        origin_turn_id=FIX["request"]["turn_id"], target_chat_id=SCENARIOS["execution_chat_id"],
        prompt="same durable request", notify_origin=False)
    with SQLiteToolState(str(tmp_path / "claims.sqlite")) as store:
        async def fetch():
            return FIX["jwks"]
        kwargs = dict(registry=registry, store=store, scope="account", timeout=2, poll_interval=0.001,
                      verifier=AsyncJwksVerifier(fetch) if async_ else JwksVerifier(lambda: FIX["jwks"]))
        expected = {"unknown":NotFoundError, "submission-error":EnvaiError, "identity":ProtocolError, "receipt":ProtocolError, "signature":ToolVerificationError, "eof":TransportError}
        async def execute():
            if mode == "resume":
                return await client.chats.resume_delegation(SCENARIOS["origin_chat_id"],
                    models.TeamDelegationReceipt(**SCENARIOS["delegation_receipt"]), **kwargs)
            return await client.chats.delegate_and_wait(SCENARIOS["origin_chat_id"], body=request, **kwargs)
        def execute_sync():
            if mode == "resume":
                return client.chats.resume_delegation(SCENARIOS["origin_chat_id"],
                    models.TeamDelegationReceipt(**SCENARIOS["delegation_receipt"]), **kwargs)
            return client.chats.delegate_and_wait(SCENARIOS["origin_chat_id"], body=request, **kwargs)
        try:
            if mode in expected:
                with pytest.raises(expected[mode]):
                    await execute() if async_ else execute_sync()
            else:
                result = await execute() if async_ else execute_sync()
                assert result.output == "final job output" and result.result_turn_id == "final-synthesis"
                assert result.result_turn_id != SCENARIOS["delegation_receipt"]["turn_id"]
                assert result.status == (mode if mode in ("completed","failed","cancelled") else "completed")
                if mode in ("failed","cancelled"):
                    assert result.error == "job reason"
        finally:
            if async_:
                await client.aclose()
            else:
                client.close()
    expected_effects = int(mode in ("fresh","duplicate-running","resume","submission-error"))
    assert len(effects) == expected_effects
    assert len([r for r in requests if r.url.path.endswith("/tool-output")]) == expected_effects
    assert len([r for r in requests if r.url.path.endswith("/delegations")]) == int(mode != "resume")


@pytest.mark.parametrize("async_", [False, True])
async def test_job_stream_failure_and_deadline(async_, tmp_path):
    registry = ClientToolRegistry()
    def http(request):
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, headers=NATIVE_ACK, json={**CAPABILITIES,"features":FEATURES})
        return httpx.Response(403, headers=NATIVE_ACK, json={"error":"forbidden"})
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True, transport=httpx.MockTransport(http), retry=RetryPolicy(base_delay=0))
    with SQLiteToolState(str(tmp_path/"state.sqlite")) as store:
        kwargs = dict(registry=registry,store=store,scope="account",timeout=0.1)
        receipt = models.TeamDelegationReceipt(**SCENARIOS["delegation_receipt"])
        started = time.monotonic()
        with pytest.raises(EnvaiError):
            if async_:
                await client.chats.resume_delegation(SCENARIOS["origin_chat_id"],receipt,**kwargs)
            else:
                client.chats.resume_delegation(SCENARIOS["origin_chat_id"],receipt,**kwargs)
        assert time.monotonic()-started < 1
    if async_:
        await client.aclose()
    else:
        client.close()
