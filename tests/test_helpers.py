import copy

import httpx
import pytest
from negotiated_transport import negotiated_transport
from test_http import sample
from test_stream import wire

from envai import AsyncEnvaiClient, EnvaiClient, RetryPolicy, SQLiteToolState
from envai._schema import OPENAPI
from envai.errors import ProtocolError, TransportError
from envai.events import TurnAccumulator, parse_event


@pytest.mark.parametrize("async_", [False, True])
async def test_image_poll_success_and_download(async_):
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path.endswith("/artifacts/download"):
            return httpx.Response(200, content=b"image")
        name = (
            "ImageGenerationAcceptedResource"
            if req.method == "POST"
            else "ImageGenerationStatusResource"
        )
        payload = sample(OPENAPI["components"]["schemas"][name])
        if req.method == "GET":
            payload["data"]["status"] = "succeeded"
        return httpx.Response(200, json=payload)

    client = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", base_url="https://example.test", transport=negotiated_transport(handler)
    )
    body = sample(OPENAPI["components"]["schemas"]["ImageGenerationRequest"])
    result = (
        await client.images.generate_and_wait(body=body, poll_interval=0.001)
        if async_
        else client.images.generate_and_wait(body=body, poll_interval=0.001)
    )
    assert result.status == "succeeded"
    url = "https://example.test/api/v1/artifacts/download?artifact=a&generation=g&signature=s"
    value = (
        await client.images.download_signed_url(url)
        if async_
        else client.images.download_signed_url(url)
    )
    assert value == b"image" and "authorization" not in requests[-1].headers
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_image_timeout_and_invalid_poll_bounds(async_):
    def handler(req):
        name = (
            "ImageGenerationAcceptedResource"
            if req.method == "POST"
            else "ImageGenerationStatusResource"
        )
        return httpx.Response(200, json=sample(OPENAPI["components"]["schemas"][name]))

    client = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", transport=negotiated_transport(handler)
    )
    body = sample(OPENAPI["components"]["schemas"]["ImageGenerationRequest"])
    for timeout in (0, -1):
        with pytest.raises(ValueError):
            if async_:
                await client.images.generate_and_wait(body=body, timeout=timeout)
            else:
                client.images.generate_and_wait(body=body, timeout=timeout)
    with pytest.raises(TimeoutError if async_ else TransportError):
        if async_:
            await client.images.generate_and_wait(body=body, timeout=0.01, poll_interval=0.001)
        else:
            client.images.generate_and_wait(body=body, timeout=0.01, poll_interval=0.001)
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("bad", [False, True])
async def test_completion_numeric_paging_never_follows_foreign_links(async_, bad):
    seen = []

    def handler(req):
        seen.append(req)
        page = int(req.url.params["page"])
        payload = {
            "data": [],
            "links": {"next": "https://evil.test/page" if page == 1 else None},
            "meta": {"last_page": 1 if bad else 2},
        }
        return httpx.Response(200, json=payload)

    client = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "secret", transport=negotiated_transport(handler)
    )

    async def run():
        if async_:
            return [x async for x in client.completions.iterate("ws", "chat")]
        return list(client.completions.iterate("ws", "chat"))

    if bad:
        with pytest.raises(ProtocolError):
            await run()
    else:
        assert await run() == [] and len(seen) == 2
    assert all(r.url.host == "localhost" for r in seen)
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_get_network_retry_and_status_exhaustion(async_):
    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) <= 2:
            raise httpx.ReadError("network")
        return httpx.Response(200, json={"data": []})

    client = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", transport=negotiated_transport(handler), retry=RetryPolicy(base_delay=0)
    )
    assert (await client.list_workspaces() if async_ else client.list_workspaces()) == []
    assert len(seen) == 3
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_only_matching_turn_terminal(valid_events, async_):
    events = copy.deepcopy(valid_events)
    terminal = next(e for e in events if e["type"] == "turn.completed")
    old = copy.deepcopy(terminal)
    old["turn_id"] = old["data"]["turn_id"] = "cmpl_01ARZ3NDEKTSV4RRFFQ69G5FAA"
    source = [events[1], events[0], old, terminal]
    for index, event in enumerate([e for e in source if e["type"] != "ping"], 1):
        event["seq"] = index

    def handler(req):
        if req.method == "POST":
            return httpx.Response(202, json={"status": "accepted", "turn_id": terminal["turn_id"]})
        return httpx.Response(
            200, content=wire(source), headers={"content-type": "text/event-stream"}
        )

    client = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", transport=negotiated_transport(handler)
    )
    result = (
        await client.chats.send_and_wait(terminal["chat_id"], "hi")
        if async_
        else client.chats.send_and_wait(terminal["chat_id"], "hi")
    )
    assert result.turn_id == terminal["turn_id"] and result.cost == terminal["data"]["cost"]
    if async_:
        await client.aclose()
    else:
        client.close()


def test_sqlite_context_manager(tmp_path):
    with SQLiteToolState(str(tmp_path / "state.db")) as store:
        assert store.claim("scope", "chat", "call", "hash", "turn")


def test_text_done_overrides_deltas(valid_events):
    delta = copy.deepcopy(next(e for e in valid_events if e["type"] == "agent.text.delta"))
    acc = TurnAccumulator()
    acc.accept(parse_event(delta))
    delta["type"] = "agent.text.done"
    delta["data"] = {"text": "final"}
    acc.accept(parse_event(delta))
    assert acc.text == "final"
