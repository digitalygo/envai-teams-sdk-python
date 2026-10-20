from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from envai import AsyncEnvaiClient, EnvaiClient, models
from envai.errors import CompatibilityError, ConflictError, TransportError

FIXTURES = Path(__file__).parent / "fixtures"
CAPABILITY_FIXTURE = json.loads(
    (FIXTURES / "capabilities" / "canonical-v3.json").read_text()
)
CAPABILITIES = CAPABILITY_FIXTURE["body"]
READY = json.loads((FIXTURES / "chat-attachments" / "ready.json").read_text())["data"]
ORIGINAL = copy.deepcopy(READY)
ORIGINAL["conversion"]["status"] = "not_required"
ORIGINAL["derived"] = None
WIKI_SOURCE = json.loads((FIXTURES / "wiki" / "source-accepted.json").read_text())["data"]
ORIGINAL_SOURCE = copy.deepcopy(WIKI_SOURCE)
ORIGINAL_SOURCE["lifecycle"] = "inbox"
ORIGINAL_SOURCE["conversion"] = {"status": "not_required"}
ORIGINAL_SOURCE["indexing"] = {"status": "pending"}
ACK = {"X-Envai-Contract-Version": "3"}


def response(value, status=200):
    return httpx.Response(status, json=value, headers=ACK)


def capabilities(features):
    return {**CAPABILITIES, "features": features}


def assert_zero_mutation(requests):
    assert [request.method for request in requests] == ["GET"]
    assert requests[0].url.path == "/api/v1/atp/v3/capabilities"


def ordered_upload(content):
    return content.index(b'name="processing"') < content.index(b'name="file"')


@pytest.mark.parametrize("async_", [False, True])
def test_canonical_capability_list_allows_native_upload(tmp_path, async_):
    path = tmp_path / "notes.md"
    path.write_bytes(b"synthetic native bytes\n")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(CAPABILITIES)
        return response({"data": ORIGINAL}, 202)

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            value = await client.attachments.upload_file(
                "w", "c", path, idempotency_key="native-key"
            )
            assert value.conversion.status == "not_required"

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            value = client.attachments.upload_file(
                "w", "c", path, idempotency_key="native-key"
            )
            assert value.conversion.status == "not_required"
    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[0].url.path == "/api/v1/atp/v3/capabilities"
    assert requests[1].url.path == "/api/v1/workspaces/w/chats/c/attachments"
    assert requests[1].headers["Idempotency-Key"] == "native-key"
    assert b'filename="notes.md"' in requests[1].content
    assert b"synthetic native bytes\n" in requests[1].content


@pytest.mark.parametrize("async_", [False, True])
def test_canonical_capability_map_allows_native_and_original_uploads(tmp_path, async_):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"mapped original\n")
    requests = []
    mapped = {
        "chat_attachments": True,
        "chat_attachment_originals": True,
        "wiki_original_sources": True,
        "future_feature": True,
    }

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(capabilities(mapped))
        if request.url.path.endswith("/files/sources"):
            return response({"data": ORIGINAL_SOURCE}, 202)
        return response({"data": ORIGINAL}, 202)

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            assert (
                await client.attachments.upload_file(
                    "w", "c", path, idempotency_key="native-map"
                )
            ).conversion.status == "not_required"
            source = await client.wiki.upload_file(
                "w", path, idempotency_key="wiki-map", processing="original"
            )
            assert source.conversion.status == "not_required"
            assert (await client.capabilities.get()).features == [
                "chat_attachments",
                "chat_attachment_originals",
                "wiki_original_sources",
                "future_feature",
            ]

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            assert (
                client.attachments.upload_file(
                    "w", "c", path, idempotency_key="native-map"
                )
            ).conversion.status == "not_required"
            source = client.wiki.upload_file(
                "w", path, idempotency_key="wiki-map", processing="original"
            )
            assert source.conversion.status == "not_required"
            assert client.capabilities.get().features == [
                "chat_attachments",
                "chat_attachment_originals",
                "wiki_original_sources",
                "future_feature",
            ]
    assert [request.method for request in requests] == ["GET", "POST", "GET", "POST", "GET"]


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "features",
    [
        [],
        ["chat_attachments"],
        ["chat_attachment_originals"],
        ["chat_attachments", 1],
        ["chat_attachments", "chat_attachment_originals", 1],
        {"chat_attachments": True},
        {"chat_attachments": True, "chat_attachment_originals": False},
        {"chat_attachments": True, "chat_attachment_originals": "true"},
        {"chat_attachments": True, "chat_attachment_originals": None},
        {"chat_attachments": True, "chat_attachment_originals_beta": True},
        {"chat_attachments": True, "chat_attachment_originals": True, "future": 1},
    ],
)
def test_native_upload_fails_closed_before_multipart(tmp_path, async_, features):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"must not upload\n")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(capabilities(features))
        raise AssertionError("Multipart mutation must not be sent")

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CompatibilityError):
                await client.attachments.upload_file(
                    "w", "c", path, idempotency_key="denied"
                )

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CompatibilityError):
                client.attachments.upload_file("w", "c", path, idempotency_key="denied")
    assert_zero_mutation(requests)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        [],
        {key: value for key, value in CAPABILITIES.items() if key != "features"},
        capabilities(None),
        capabilities("chat_attachments"),
    ],
)
def test_malformed_capability_payload_never_mutates(tmp_path, async_, body):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"must not upload\n")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(body)
        raise AssertionError("Multipart mutation must not be sent")

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CompatibilityError):
                await client.attachments.upload_file(
                    "w", "c", path, idempotency_key="denied"
                )

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CompatibilityError):
                client.attachments.upload_file("w", "c", path, idempotency_key="denied")
    assert_zero_mutation(requests)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "features",
    [
        [],
        ["chat_attachments"],
        ["wiki_original_sources", 1],
        {"wiki_original_sources": False},
        {"wiki_original_sources": "true"},
        {"wiki_original_sources": None},
        {"wiki_original_sources_beta": True},
        {"wiki_original_sources": True, "chat_attachments": 1},
    ],
)
def test_original_wiki_upload_fails_closed_before_multipart(tmp_path, async_, features):
    path = tmp_path / "source.txt"
    path.write_bytes(b"must not upload\n")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(capabilities(features))
        raise AssertionError("Multipart mutation must not be sent")

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CompatibilityError):
                await client.wiki.upload_file(
                    "w", path, idempotency_key="denied", processing="original"
                )

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CompatibilityError):
                client.wiki.upload_file(
                    "w", path, idempotency_key="denied", processing="original"
                )
    assert_zero_mutation(requests)


@pytest.mark.parametrize("async_", [False, True])
def test_original_wiki_high_level_upload_preserves_bytes_and_order(tmp_path, async_):
    path = tmp_path / "source.txt"
    path.write_bytes(b"original wiki bytes\n")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(capabilities(CAPABILITIES["features"]))
        return response({"data": ORIGINAL_SOURCE}, 202)

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            source = await client.wiki.upload_file(
                "w", path, idempotency_key="original-key", processing="original"
            )
            assert source.conversion.status == "not_required"

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            source = client.wiki.upload_file(
                "w", path, idempotency_key="original-key", processing="original"
            )
            assert source.conversion.status == "not_required"
    assert [request.method for request in requests] == ["GET", "POST"]
    upload = requests[1]
    assert upload.headers["Idempotency-Key"] == "original-key"
    assert ordered_upload(upload.content)
    assert b'filename="source.txt"' in upload.content
    assert b"original wiki bytes\n" in upload.content


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("processing", [None, "convert"])
def test_wiki_default_and_convert_uploads_are_unchanged(tmp_path, async_, processing):
    path = tmp_path / "source.txt"
    path.write_bytes(b"converted wiki bytes\n")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(capabilities([]))
        return response({"data": WIKI_SOURCE}, 202)

    kwargs = {} if processing is None else {"processing": processing}

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            source = await client.wiki.upload_file(
                "w", path, idempotency_key="convert-key", **kwargs
            )
            assert source.conversion.status == "queued"

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            source = client.wiki.upload_file("w", path, idempotency_key="convert-key", **kwargs)
            assert source.conversion.status == "queued"
    assert [request.method for request in requests] == ["GET", "POST"]
    upload = requests[1]
    assert b'name="processing"' not in upload.content
    assert b'filename="source.txt"' in upload.content
    assert b"converted wiki bytes\n" in upload.content


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("processing", ["ocr", "", None, True, 1, []])
def test_invalid_high_level_processing_never_reaches_transport(tmp_path, async_, processing):
    path = tmp_path / "source.txt"
    path.write_bytes(b"invalid mode\n")
    requests = []

    def handler(request):
        requests.append(request)
        raise AssertionError("Invalid mode must not reach transport")

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await client.wiki.upload_file(
                    "w", path, idempotency_key="invalid", processing=processing
                )

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                client.wiki.upload_file(
                    "w", path, idempotency_key="invalid", processing=processing
                )
    assert requests == []


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("data", [{"processing": "ocr"}, {"processing": None}, {"processing": 1}, "ocr"])
def test_invalid_low_level_processing_never_reaches_transport(async_, data):
    requests = []

    def handler(request):
        requests.append(request)
        raise AssertionError("Invalid mode must not reach transport")

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await client.wiki.upload(
                    "w",
                    files={"file": ("source.txt", b"invalid", "text/plain")},
                    data=data,
                    idempotency_key="invalid",
                )

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                client.wiki.upload(
                    "w",
                    files={"file": ("source.txt", b"invalid", "text/plain")},
                    data=data,
                    idempotency_key="invalid",
                )
    assert requests == []


@pytest.mark.parametrize("async_", [False, True])
def test_low_level_original_upload_cannot_bypass_capability(async_):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(capabilities(CAPABILITIES["features"]))
        return response({"data": ORIGINAL_SOURCE}, 202)

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            source = await client.wiki.upload(
                "w",
                files={"file": ("source.txt", b"low-level original\n", "text/plain")},
                data={"processing": "original"},
                idempotency_key="low-level",
            )
            assert source.conversion.status == "not_required"

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            source = client.wiki.upload(
                "w",
                files={"file": ("source.txt", b"low-level original\n", "text/plain")},
                data={"processing": "original"},
                idempotency_key="low-level",
            )
            assert source.conversion.status == "not_required"
    assert [request.method for request in requests] == ["GET", "POST"]
    assert ordered_upload(requests[1].content)
    assert b"low-level original\n" in requests[1].content


@pytest.mark.parametrize("async_", [False, True])
def test_low_level_original_upload_requires_wiki_capability(async_):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(capabilities(["chat_attachments", "chat_attachment_originals"]))
        raise AssertionError("Multipart mutation must not be sent")

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CompatibilityError):
                await client.wiki.upload(
                    "w",
                    files={"file": ("source.txt", b"denied", "text/plain")},
                    data={"processing": "original"},
                    idempotency_key="low-level",
                )

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CompatibilityError):
                client.wiki.upload(
                    "w",
                    files={"file": ("source.txt", b"denied", "text/plain")},
                    data={"processing": "original"},
                    idempotency_key="low-level",
                )
    assert_zero_mutation(requests)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "sequence,expected_calls",
    [
        ("uploaded_ready", 2),
        ("ready", 1),
    ],
)
def test_not_required_polling_requires_ready_lifecycle(async_, sequence, expected_calls):
    uploaded = copy.deepcopy(ORIGINAL)
    uploaded["lifecycle"] = "uploaded"
    values = [uploaded, ORIGINAL] if sequence == "uploaded_ready" else [ORIGINAL]
    reads = []

    def handler(request):
        reads.append(request)
        return response({"data": values.pop(0)})

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            value = await client.attachments.wait("w", "c", "a", interval=0.001, timeout=1)
            assert value.lifecycle == "ready"
            assert value.conversion.status == "not_required"

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            value = client.attachments.wait("w", "c", "a", interval=0.001, timeout=1)
            assert value.lifecycle == "ready"
            assert value.conversion.status == "not_required"
    assert len(reads) == expected_calls


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "status,recoverable,lifecycle",
    [
        ("completed", False, "ready"),
        ("failed", False, "uploaded"),
        ("queued", False, "deleted"),
        ("queued", False, "deleting"),
    ],
)
def test_terminal_attachment_polling_states_are_preserved(
    async_, status, recoverable, lifecycle
):
    value = copy.deepcopy(ORIGINAL)
    value["conversion"]["status"] = status
    value["conversion"]["recoverable"] = recoverable
    value["lifecycle"] = lifecycle
    reads = []

    def handler(request):
        reads.append(request)
        return response({"data": value})

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            assert (
                await client.attachments.wait("w", "c", "a", interval=0.001, timeout=1)
            ).lifecycle == lifecycle

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            assert (
                client.attachments.wait("w", "c", "a", interval=0.001, timeout=1)
            ).lifecycle == lifecycle
    assert len(reads) == 1


@pytest.mark.parametrize("async_", [False, True])
def test_recoverable_attachment_failure_keeps_polling(async_):
    recoverable = copy.deepcopy(ORIGINAL)
    recoverable["conversion"]["status"] = "failed"
    recoverable["conversion"]["recoverable"] = True
    terminal = copy.deepcopy(recoverable)
    terminal["conversion"]["recoverable"] = False
    reads = []

    def handler(request):
        reads.append(request)
        return response({"data": recoverable if len(reads) == 1 else terminal})

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            value = await client.attachments.wait("w", "c", "a", interval=0.001, timeout=1)
            assert value.conversion.status == "failed"
            assert not value.conversion.recoverable

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            value = client.attachments.wait("w", "c", "a", interval=0.001, timeout=1)
            assert value.conversion.status == "failed"
            assert not value.conversion.recoverable
    assert len(reads) == 2


@pytest.mark.parametrize("async_", [False, True])
def test_wiki_status_and_attachment_manifest_hydrate_not_required(async_):
    def handler(request):
        assert request.url.path == "/api/v1/workspaces/w/files/sources/file"
        return response({"data": ORIGINAL_SOURCE})

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            source = await client.wiki.source("w", "file")
            assert isinstance(source, models.WikiSource)
            assert source.lifecycle == "inbox"
            assert source.conversion.status == "not_required"
            assert source.indexing.status == "pending"

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            source = client.wiki.source("w", "file")
            assert isinstance(source, models.WikiSource)
            assert source.lifecycle == "inbox"
            assert source.conversion.status == "not_required"
            assert source.indexing.status == "pending"
    manifest = models.AttachmentManifest.model_validate(
        {
            "attachment_id": "catt_native",
            "original_name": "notes.md",
            "mime_type": "text/markdown",
            "mode": "native",
            "revision": 1,
            "sha256": "a" * 64,
            "bytes": 17,
            "conversion": {"status": "not_required"},
        }
    )
    assert manifest.mime_type == "text/markdown"
    assert manifest.mode == "native"
    assert manifest.conversion["status"] == "not_required"
    assert (
        models.AttachmentConversion(
            status="not_required",
            error_code=None,
            recoverable=False,
            next_retry_at=None,
            retryable=False,
        ).status
        == "not_required"
    )


@pytest.mark.parametrize("async_", [False, True])
def test_existing_attachment_operations_need_only_chat_attachments(async_):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(capabilities(["chat_attachments"]))
        if request.url.path.endswith("/messages"):
            return response(
                {"status": "accepted", "turn_id": "cmpl_01j5abcdefghjkmnpqrstvwxyz", "attachment_ids": ["a"]}, 202
            )
        if request.url.path.endswith("/download"):
            return httpx.Response(200, content=b"original", headers=ACK)
        if request.method == "GET" and request.url.path.endswith("/attachments"):
            return response({"data": {"attachments": [ORIGINAL]}})
        return response({"data": ORIGINAL})

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            await client.attachments.get("w", "c", "a")
            await client.attachments.list("w", "c")
            await client.attachments.delete("w", "c", "a")
            await client.attachments.retry("w", "c", "a", idempotency_key="retry")
            assert await client.attachments.download("w", "c", "a", kind="original") == b"original"
            await client.chats.send_message(
                "c",
                body={"message": "hello", "attachment_ids": ["a"]},
                idempotency_key="message",
            )

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            client.attachments.get("w", "c", "a")
            client.attachments.list("w", "c")
            client.attachments.delete("w", "c", "a")
            client.attachments.retry("w", "c", "a", idempotency_key="retry")
            assert client.attachments.download("w", "c", "a", kind="original") == b"original"
            client.chats.send_message(
                "c",
                body={"message": "hello", "attachment_ids": ["a"]},
                idempotency_key="message",
            )
    assert len([request for request in requests if request.url.path.endswith("/capabilities")]) == 3


@pytest.mark.parametrize("async_", [False, True])
def test_native_upload_is_not_resubmitted_and_retry_409_is_preserved(async_):
    path = Path(__file__).parent / "fixtures" / "chat-attachments" / "ready.json"
    requests = []
    posts = 0

    def handler(request):
        nonlocal posts
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return response(capabilities(["chat_attachments", "chat_attachment_originals"]))
        posts += 1
        if posts == 1:
            raise httpx.ReadError("ambiguous upload")
        if request.url.path.endswith("/retry"):
            return httpx.Response(
                409,
                json={
                    "error": {
                        "type": "conflict",
                        "code": "attachment_retry_not_permitted",
                        "message": "Native retry is not permitted",
                    }
                },
                headers=ACK,
            )
        return response({"data": ORIGINAL}, 202)

    async def run():
        async with AsyncEnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TransportError):
                await client.attachments.upload_file(
                    "w", "c", path, idempotency_key="same-key"
                )
            with pytest.raises(ConflictError) as caught:
                await client.attachments.retry("w", "c", "a", idempotency_key="retry-key")
            assert caught.value.code == "attachment_retry_not_permitted"

    if async_:
        asyncio.run(run())
    else:
        with EnvaiClient("key", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TransportError):
                client.attachments.upload_file("w", "c", path, idempotency_key="same-key")
            with pytest.raises(ConflictError) as caught:
                client.attachments.retry("w", "c", "a", idempotency_key="retry-key")
            assert caught.value.code == "attachment_retry_not_permitted"
    uploads = [request for request in requests if request.method == "POST"]
    assert len(uploads) == 2
    assert uploads[0].url.path.endswith("/attachments")
    assert uploads[0].headers["Idempotency-Key"] == "same-key"
    assert uploads[1].url.path.endswith("/retry")
    assert uploads[1].headers["Idempotency-Key"] == "retry-key"


def test_canonical_capability_fixture_matches_provenance():
    provenance = json.loads((FIXTURES / "capabilities" / "provenance.json").read_text())
    raw = (FIXTURES / "capabilities" / "canonical-v3.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == provenance["files"]["canonical-v3.json"]
    assert provenance["source_repository"] == "digitalygo/envai-teams"
    assert provenance["source_path"] == "apps/execution-plane/internal/gateway/capabilities.go"
    assert provenance["source_commit"] == "7192fc05ebec817bddc705186c8f195cc0dc5ec5"
    assert provenance["php_reference"]["commit"] == "93eafc02271035065ac975f91cd4f77d148c9d2c"
    assert provenance["captured_from"] == "GET /api/v1/atp/v3/capabilities"
    assert "chat_attachment_originals" in CAPABILITIES["features"]
    assert "wiki_original_sources" in CAPABILITIES["features"]


def test_generated_native_original_fields_are_optional_additions():
    manifest = models.AttachmentManifest.model_fields
    assert not manifest["mime_type"].is_required()
    assert not manifest["mode"].is_required()
    assert "not_required" in models.AttachmentConversion.model_fields["status"].annotation.__args__
    assert "not_required" in models.ConversionState.model_fields["status"].annotation.__args__
