import asyncio
import copy
import json
import threading

import httpx
import pytest

from envai import AsyncEnvaiClient, EnvaiClient, RetryPolicy
from envai.errors import (
    AuthenticationError,
    CancelledError,
    ProtocolError,
    ReplayGapError,
    TransportError,
)


def wire(events):
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


@pytest.mark.parametrize("async_", [False, True])
async def test_stream_resume_duplicate_ping(valid_events, async_):
    events = valid_events[:4]
    seen = []

    def handler(req):
        seen.append(req)
        batch = events[:3] if len(seen) == 1 else [events[2], events[0], events[3]]
        return httpx.Response(
            200, content=wire(batch), headers={"content-type": "text/event-stream"}
        )

    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", transport=httpx.MockTransport(handler), retry=RetryPolicy(base_delay=0)
    )
    stream = c.chats.events(events[0]["chat_id"])
    result = []
    for i in range(5):
        result.append(await anext(stream) if async_ else next(stream))
    assert [e.seq for e in result] == [0, 1, 2, 0, 3]
    assert stream.last_seq == 3 and stream.live
    assert seen[1].url.params["after"] == "2"
    if async_:
        await stream.aclose()
        await c.aclose()
    else:
        stream.close()
        c.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_gap_recovery_and_failure(valid_events, async_):
    for recover in (True, False):
        seen = []

        def handler(req):
            seen.append(req)
            batch = (
                [valid_events[1], valid_events[3]]
                if len(seen) == 1
                else valid_events[2:4]
                if recover
                else [valid_events[3]]
            )
            return httpx.Response(
                200, content=wire(batch), headers={"content-type": "text/event-stream"}
            )

        c = (AsyncEnvaiClient if async_ else EnvaiClient)(
            "x", transport=httpx.MockTransport(handler), retry=RetryPolicy(base_delay=0)
        )
        stream = c.chats.events(valid_events[1]["chat_id"])
        first = await anext(stream) if async_ else next(stream)
        assert first.seq == 1
        if recover:
            second = await anext(stream) if async_ else next(stream)
            assert second.seq == 2
        else:
            with pytest.raises(ReplayGapError) as error:
                await anext(stream) if async_ else next(stream)
            assert error.value.expected == 2 and error.value.received == 3
        assert seen[1].url.params["after"] == "1"
        if async_:
            await stream.aclose()
            await c.aclose()
        else:
            stream.close()
            c.close()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "case", ["401", "content", "version", "chat", "eof", "network", "cancel", "closed", "limit"]
)
async def test_stream_stops(valid_events, async_, case):
    event = copy.deepcopy(valid_events[1])
    if case == "version":
        event["v"] = 3
    if case == "chat":
        event["chat_id"] = "cht_01ARZ3NDEKTSV4RRFFQ69G5FAA"

    def handler(req):
        if case == "network":
            raise httpx.ReadError("network")
        return httpx.Response(
            401 if case == "401" else 200,
            content=b"" if case in ("eof", "401") else wire([event]),
            headers={"content-type": "text/plain" if case == "content" else "text/event-stream"},
        )

    c = (AsyncEnvaiClient if async_ else EnvaiClient)("x", transport=httpx.MockTransport(handler))
    cancel = threading.Event()
    stream = c.chats.events(valid_events[1]["chat_id"], max_reconnects=0, cancel=cancel)
    if case == "cancel":
        cancel.set()
    if case == "limit":
        stream.max_duration = 0.001
        stream._started -= 1
    if case == "closed":
        if async_:
            await stream.aclose()
        else:
            stream.close()
    exc = {
        "401": AuthenticationError,
        "content": ProtocolError,
        "version": ProtocolError,
        "chat": ProtocolError,
        "eof": TransportError,
        "network": TransportError,
        "cancel": CancelledError,
        "closed": StopAsyncIteration if async_ else StopIteration,
        "limit": TransportError,
    }[case]
    with pytest.raises(exc):
        await anext(stream) if async_ else next(stream)
    if async_:
        await stream.aclose()
        await c.aclose()
    else:
        stream.close()
        c.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_matching_turn_helper_waits_ping(valid_events, async_):
    sent = []
    events = copy.deepcopy(valid_events)
    # Include the canonical ping, started/text events and matching terminal.
    events = [e for e in events if e["type"] not in ("agent.tool.request", "future.vendor.event")]
    for n, event in enumerate([e for e in events if e["type"] != "ping"], 1):
        event["seq"] = n

    def handler(req):
        sent.append(req)
        if req.method == "POST":
            assert sent[0].method == "GET"
            return httpx.Response(
                202, json={"status": "accepted", "turn_id": valid_events[1]["turn_id"]}
            )
        return httpx.Response(
            200, content=wire(events), headers={"content-type": "text/event-stream"}
        )

    c = (AsyncEnvaiClient if async_ else EnvaiClient)("x", transport=httpx.MockTransport(handler))
    result = (
        await c.chats.send_and_wait(valid_events[1]["chat_id"], "hi")
        if async_
        else c.chats.send_and_wait(valid_events[1]["chat_id"], "hi")
    )
    assert result.text == "hello" and result.terminal_event.terminal
    assert result.usage.input_tokens >= 0
    if async_:
        await c.aclose()
    else:
        c.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_single_reader_and_context_cleanup(valid_events, async_):
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, content=wire(valid_events[:2]), headers={"content-type": "text/event-stream"}
            )
        ),
    )
    stream = c.chats.events(valid_events[1]["chat_id"])
    stream._lock.acquire()
    with pytest.raises(RuntimeError):
        await anext(stream) if async_ else next(stream)
    stream._lock.release()
    if async_:
        async with stream:
            assert (await anext(stream)).seq == 0
        await c.aclose()
    else:
        with stream:
            assert next(stream).seq == 0
        c.close()
    assert stream.closed


async def test_async_task_cancel_releases_stream(valid_events):
    ended = asyncio.Event()

    class Blocking(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                await asyncio.Event().wait()
                yield b""
            finally:
                ended.set()

    c = AsyncEnvaiClient(
        "x",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, stream=Blocking(), headers={"content-type": "text/event-stream"}
            )
        ),
    )
    stream = c.chats.events(valid_events[1]["chat_id"])
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ended.wait()
    await stream.aclose()
    await c.aclose()


def test_remaining_stream_budget_bounds_connection_and_backoff(valid_events):
    with EnvaiClient(
        "x",
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        retry=RetryPolicy(max_delay=10),
    ) as client:
        stream = client.chats.events(valid_events[1]["chat_id"], max_duration=0.5)
        request = stream._request()
        assert 0 < request["timeout"].read <= 0.5
        assert 0 < request["timeout"].connect <= 0.5
        response = httpx.Response(503, headers={"retry-after": "10"})
        try:
            stream._status(response)
        except Exception as exc:
            assert 0 <= stream._retry(exc) <= 0.5
        stream.close()
        for options in ({"max_duration": float("nan")}, {"after": 1.2}, {"max_reconnects": 1.2}):
            with pytest.raises(ValueError):
                client.chats.events(valid_events[1]["chat_id"], **options)
