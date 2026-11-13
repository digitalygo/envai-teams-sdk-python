import json
from types import SimpleNamespace

import httpx
import pytest
from negotiated_transport import negotiated_transport

from envai import AsyncEnvaiClient, EnvaiClient, models
from envai.errors import EnvaiError, ValidationError
from envai.events import parse_event


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("helper", ["send_and_stream", "send_and_wait"])
async def test_helpers_forward_model_per_turn(async_mode, helper, monkeypatch, valid_events):
    ping = parse_event(next(e for e in valid_events if e["type"] == "ping"))
    terminal = parse_event(next(e for e in valid_events if e["type"] == "turn.completed"))
    sent = []

    class Stream:
        def __enter__(self):
            return iter([ping, terminal])

        def __exit__(self, *args):
            return False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def __aiter__(self):
            for event in [ping, terminal]:
                yield event

    def events(*args, **kwargs):
        assert "model" not in kwargs
        return Stream()

    def send(chat_id, **kwargs):
        sent.append(kwargs)
        return SimpleNamespace(turn_id=terminal.turn_id)

    async def send_async(chat_id, **kwargs):
        return send(chat_id, **kwargs)

    client = (AsyncEnvaiClient if async_mode else EnvaiClient)("key")
    monkeypatch.setattr(client.chats, "events", events)
    monkeypatch.setattr(client.chats, "send_message", send_async if async_mode else send)
    for kwargs in ({"model": "Vendor/Exact", "attachment_ids": ["a"], "idempotency_key": "k"}, {}):
        result = getattr(client.chats, helper)("c", "hello", **kwargs)
        if async_mode:
            if helper == "send_and_stream":
                assert len([event async for event in result]) == 1
            else:
                await result
        elif helper == "send_and_stream":
            assert len(list(result)) == 1
        else:
            pass
    assert sent[0]["body"] == {"message": "hello", "attachment_ids": ["a"], "model": "Vendor/Exact"}
    assert sent[0]["idempotency_key"] == "k"
    assert sent[1]["body"] == {"message": "hello", "attachment_ids": []}
    if async_mode:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("value", [None, 2, True, [], {}, "", " x", "x ", "\tx"])
async def test_invalid_model_rejected_without_send(async_mode, value):
    sent = []
    client = (AsyncEnvaiClient if async_mode else EnvaiClient)(
        "key", transport=negotiated_transport(lambda request: sent.append(request) or httpx.Response(500))
    )
    with pytest.raises(ValidationError):
        result = client.chats.send_message("c", body={"message": "hi", "model": value})
        if async_mode:
            await result
    assert not sent
    if async_mode:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("status,code", [(422, "invalid_model"), (422, "unknown_model"),
    (422, "model_unavailable"), (422, "model_incompatible"),
    (503, "model_catalog_unavailable"), (409, "idempotency_conflict")])
async def test_model_errors_preserved_without_resend(async_mode, status, code):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(status, json={"error": {"type": "validation_error", "code": code, "message": "no"}})

    client = (AsyncEnvaiClient if async_mode else EnvaiClient)("key", transport=negotiated_transport(handler))
    with pytest.raises(EnvaiError) as caught:
        result = client.chats.send_message("c", body={"message": "hi", "model": "X/Y"})
        if async_mode:
            await result
    assert caught.value.code == code and caught.value.http_status == status
    assert len(sent) == 1 and json.loads(sent[0].content)["model"] == "X/Y"
    if async_mode:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("strict", [None, False, True])
async def test_explicit_model_with_strict_policy(async_mode, strict):
    from test_chat_attachments import CAPS, response

    sent = []

    def handler(request):
        if request.url.path.endswith("/capabilities"):
            result = response({**CAPS, "features": ["detached_background", "strict_model"]})
        else:
            sent.append(request)
            result = response({"status": "accepted", "turn_id": "cmpl_01j5abcdefghjkmnpqrstvwxyz"}, 202)
        if strict is True:
            result.headers["X-Envai-Extensions"] = "native-tasks-v1"
        return result

    client = (AsyncEnvaiClient if async_mode else EnvaiClient)(
        "key", native_tasks=strict is True, transport=httpx.MockTransport(handler))
    body = {"message": "hi", "model": "Vendor/Exact"}
    if strict is not None:
        body["strict_model"] = strict
    result = client.chats.send_message("c", body=body)
    if async_mode:
        await result
    assert len(sent) == 1 and json.loads(sent[0].content) == body
    if async_mode:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_mode", [False, True])
async def test_catalog_retains_both_selectable_values(async_mode):
    entry = {"id": "vendor/a", "object": "model", "created": 1, "owned_by": "vendor",
             "type": "llm", "deprecated": False, "sunset_at": None}
    payload = {"object": "list", "data": [{**entry, "selectable": True},
               {**entry, "id": "vendor/b", "selectable": False}],
               "defaults": {"orchestrator": "vendor/a", "subagent": "vendor/a"}}
    client = (AsyncEnvaiClient if async_mode else EnvaiClient)(
        "key", transport=negotiated_transport(lambda request: httpx.Response(200, json=payload)))
    result = client.models.list()
    if async_mode:
        result = await result
    assert [(item.id, item.selectable) for item in result.data] == [
        ("vendor/a", True), ("vendor/b", False)]
    if async_mode:
        await client.aclose()
    else:
        client.close()


def test_dto_preserves_omission_and_null():
    assert "model" not in models.ChatMessageRequest(message="hi").model_dump(exclude_unset=True)
    assert models.ChatMessageRequest(message="hi", model=None).model_dump(exclude_unset=True)["model"] is None
