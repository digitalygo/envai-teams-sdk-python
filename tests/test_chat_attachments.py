from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import httpx
import pytest

from envai import AsyncEnvaiClient, EnvaiClient
from envai._transport import RequestOptions
from envai.errors import CancelledError, CompatibilityError, ProtocolError, TransportError

ROOT = Path(__file__).parent / "fixtures" / "chat-attachments"
READY = json.loads((ROOT / "ready.json").read_text())["data"]
PENDING = json.loads((ROOT / "pending.json").read_text())["data"]
FAILED = json.loads((ROOT / "failed.json").read_text())["data"]
CAPS = {
    "protocol_version": 3,
    "contract_version": 3,
    "supported_contract_versions": [2, 3],
    "legacy_v2_mode": "historical_reads_replay_only",
    "families": [],
    "event_types": [],
    "tool_kinds": [],
    "features": ["chat_attachments", "chat_attachment_originals"],
}


def response(value, status=200):
    return httpx.Response(status, json=value, headers={"X-Envai-Contract-Version": "3"})


@pytest.mark.parametrize("async_mode", [False, True])
def test_attachment_services_scope_multipart_and_no_automatic_mutation_retry(tmp_path, async_mode):
    requests = []
    path = tmp_path / "notes.txt"
    path.write_text("synthetic")

    def handler(request):
        if request.url.path.endswith("/capabilities"):
            return response(CAPS)
        requests.append(request)
        assert request.headers["X-Envai-Contract-Version"] == "3"
        if "/download" in request.url.path:
            assert request.headers["Accept"] == "application/octet-stream"
            return httpx.Response(
                200, content=b"original", headers={"X-Envai-Contract-Version": "3"}
            )
        return response(
            {
                "data": {"attachments": [READY]}
                if request.method == "GET" and request.url.path.endswith("attachments")
                else READY
            }
        )

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            assert (
                await client.attachments.upload_file("w/a", "c/b", path, idempotency_key="upload")
            ).attachment_id == READY["attachment_id"]
            assert (await client.attachments.list("w/a", "c/b")).attachments[0].lifecycle == "ready"
            assert (await client.attachments.get("w/a", "c/b", "a")).lifecycle == "ready"
            assert (
                await client.attachments.retry("w/a", "c/b", "a", idempotency_key="retry")
            ).lifecycle == "ready"
            assert (
                await client.attachments.download("w/a", "c/b", "a", kind="original") == b"original"
            )
            assert (await client.attachments.delete("w/a", "c/b", "a")).lifecycle == "ready"

    if async_mode:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            assert (
                client.attachments.upload_file(
                    "w/a", "c/b", path, idempotency_key="upload"
                ).attachment_id
                == READY["attachment_id"]
            )
            assert client.attachments.list("w/a", "c/b").attachments[0].lifecycle == "ready"
            client.attachments.get("w/a", "c/b", "a")
            client.attachments.retry("w/a", "c/b", "a", idempotency_key="retry")
            assert client.attachments.download("w/a", "c/b", "a", kind="original") == b"original"
            client.attachments.delete("w/a", "c/b", "a")
    assert len(requests) == 6
    assert all("w%2Fa/chats/c%2Fb/attachments" in str(r.url) for r in requests)
    assert requests[0].headers["Idempotency-Key"] == "upload"
    assert b'name="file"' in requests[0].content
    assert requests[3].headers["Idempotency-Key"] == "retry"


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize(
    "failure", ["no_feature", "ack_missing", "ack_changed", "network", "no_key"]
)
def test_messages_fail_closed_and_never_resubmit(async_mode, failure):
    posts = []

    def handler(request):
        if request.url.path.endswith("/capabilities"):
            return response({**CAPS, "features": []} if failure == "no_feature" else CAPS)
        posts.append(request)
        assert json.loads(request.content)["attachment_ids"] == ["a"]
        assert request.headers["Idempotency-Key"] == "stable"
        if failure == "network":
            raise httpx.ReadError("synthetic interruption")
        return response(
            {
                "status": "accepted",
                "turn_id": "cmpl_test",
                **({"attachment_ids": ["b"]} if failure == "ack_changed" else {}),
            },
            202,
        )

    expected = (
        ValueError
        if failure == "no_key"
        else CompatibilityError
        if failure == "no_feature"
        else TransportError
        if failure == "network"
        else ProtocolError
    )
    kwargs = {
        "body": {"message": "hello", "attachment_ids": ["a"]},
        "idempotency_key": None if failure == "no_key" else "stable",
    }

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(expected):
                await client.chats.send_message("c", **kwargs)

    if async_mode:
        asyncio.run(run())
    else:
        with (
            EnvaiClient("key", transport=httpx.MockTransport(handler)) as client,
            pytest.raises(expected),
        ):
            client.chats.send_message("c", **kwargs)
    assert len(posts) == (0 if failure in ("no_key", "no_feature") else 1)


@pytest.mark.parametrize("async_mode", [False, True])
def test_polling_success_failure_timeout_and_cancellation(tmp_path, async_mode):
    sequence = [PENDING, READY, FAILED]

    def handler(request):
        return response({"data": sequence.pop(0) if sequence else PENDING})

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            assert (
                await client.attachments.wait("w", "c", "a", interval=0.001)
            ).lifecycle == "ready"
            assert (await client.attachments.wait("w", "c", "a")).conversion.retryable
            with pytest.raises(TimeoutError):
                await client.attachments.wait("w", "c", "a", interval=0.001, timeout=0.005)
            with pytest.raises(ValueError):
                await client.attachments.wait("w", "c", "a", timeout=0)
            with pytest.raises(CancelledError):
                await client.attachments.wait("w", "c", "a", request_options=cancelled)
            with pytest.raises(CancelledError):
                await client.attachments.upload_file(
                    "w", "c", tmp_path / "missing", idempotency_key="u", request_options=cancelled
                )

    cancel = threading.Event()
    cancel.set()
    cancelled = RequestOptions(cancel=cancel)
    if async_mode:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            assert client.attachments.wait("w", "c", "a", interval=0.001).lifecycle == "ready"
            assert client.attachments.wait("w", "c", "a").conversion.retryable
            with pytest.raises(TimeoutError):
                client.attachments.wait("w", "c", "a", interval=0.001, timeout=0.005)
            with pytest.raises(ValueError):
                client.attachments.wait("w", "c", "a", timeout=float("nan"))
            with pytest.raises(CancelledError):
                client.attachments.wait("w", "c", "a", request_options=cancelled)
            with pytest.raises(CancelledError):
                client.attachments.upload_file(
                    "w", "c", tmp_path / "missing", idempotency_key="u", request_options=cancelled
                )


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("replayed", [False, True, "overflow"])
def test_stream_helpers_retain_attachment_ids_and_key(monkeypatch, valid_events, async_mode, replayed):
    from types import SimpleNamespace

    from envai.events import parse_event

    ping = parse_event(next(value for value in valid_events if value["type"] == "ping"))
    terminal = parse_event(
        next(value for value in valid_events if value["type"] == "turn.completed")
    )
    sent = []
    sequence = [terminal, ping] if replayed else [ping, terminal]
    if replayed == "overflow":
        from dataclasses import replace

        sequence = [replace(terminal, raw={"payload": "x" * (17 * 1024 * 1024)})]

    class SyncStream:
        def __enter__(self):
            return iter(sequence)

        def __exit__(self, *args):
            return False

    class AsyncStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def __aiter__(self):
            for value in sequence:
                yield value

    def send(chat_id, **kwargs):
        sent.append((chat_id, kwargs))
        return SimpleNamespace(turn_id=terminal.turn_id)

    async def send_async(chat_id, **kwargs):
        return send(chat_id, **kwargs)

    async def run():
        async with AsyncEnvaiClient("synthetic") as client:
            monkeypatch.setattr(client.chats, "events", lambda *a, **k: AsyncStream())
            monkeypatch.setattr(client.chats, "send_message", send_async)
            if replayed == "overflow":
                with pytest.raises(ProtocolError) as caught:
                    await client.chats.send_and_wait("c", "Read", attachment_ids=["a"], idempotency_key="stable")
                assert "exceeds 16 MiB" in caught.value.message
            else:
                await client.chats.send_and_wait(
                    "c", "Read", attachment_ids=["a"], idempotency_key="stable"
                )

    if async_mode:
        asyncio.run(run())
    else:
        with EnvaiClient("synthetic") as client:
            monkeypatch.setattr(client.chats, "events", lambda *a, **k: SyncStream())
            monkeypatch.setattr(client.chats, "send_message", send)
            if replayed == "overflow":
                with pytest.raises(ProtocolError) as caught:
                    client.chats.send_and_wait("c", "Read", attachment_ids=["a"], idempotency_key="stable")
                assert "exceeds 16 MiB" in caught.value.message
            else:
                client.chats.send_and_wait("c", "Read", attachment_ids=["a"], idempotency_key="stable")
    if replayed == "overflow":
        assert sent == []
        return
    assert sent == [
        ("c", {"body": {"message": "Read", "attachment_ids": ["a"]}, "idempotency_key": "stable"})
    ]


@pytest.mark.parametrize("async_mode", [False, True])
def test_polling_waits_for_scheduled_recovery_without_retry(async_mode):
    recoverable = json.loads((ROOT / "recoverable.json").read_text())["data"]
    reads = []

    def handler(request):
        assert request.method == "GET"
        reads.append(request)
        return response({"data": recoverable if len(reads) == 1 else READY})

    async def run():
        async with AsyncEnvaiClient("synthetic", transport=httpx.MockTransport(handler)) as client:
            return await client.attachments.wait("w", "c", "a", interval=0.001, timeout=1)

    if async_mode:
        value = asyncio.run(run())
    else:
        with EnvaiClient("synthetic", transport=httpx.MockTransport(handler)) as client:
            value = client.attachments.wait("w", "c", "a", interval=0.001, timeout=1)
    assert value.lifecycle == "ready"
    assert len(reads) == 2
