from __future__ import annotations

import copy
import io
import json
import threading
from pathlib import Path

import httpx
import pytest
from negotiated_transport import ACK, CAPABILITIES
from test_http import sample
from test_stream import wire

from envai import AsyncEnvaiClient, EnvaiClient, RequestOptions, RetryPolicy, models
from envai._schema import OPENAPI, OPERATIONS, validate
from envai.errors import (
    CancelledError,
    CompatibilityError,
    ProtocolError,
    TransportError,
    ValidationError,
)
from envai.events import parse_event
from envai.wiki import MAX_SOURCE_BYTES, _index_options, _validate_upload

F = Path(__file__).parent / "fixtures/wiki"


def fixture(name):
    return json.loads((F / (name + ".json")).read_text())["data"]


async def close(client):
    if isinstance(client, AsyncEnvaiClient):
        await client.aclose()
    else:
        client.close()


async def invoke(client, method, *args, **kwargs):
    value = method(*args, **kwargs)
    return await value if isinstance(client, AsyncEnvaiClient) else value


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "case", ["404", "426", "missing", "wrong", "duplicate", "protocol2", "missing_cap", "bad_json"]
)
async def test_old_server_stops_before_mutation(async_, case):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/atp/v3/capabilities"
        assert request.headers["X-Envai-Contract-Version"] == "3"
        caps = copy.deepcopy(CAPABILITIES)
        headers = dict(ACK)
        if case == "protocol2":
            caps["protocol_version"] = 2
        if case == "missing_cap":
            del caps["contract_version"]
        if case == "missing":
            headers = {}
        if case == "wrong":
            headers = {"X-Envai-Contract-Version": "2"}
        if case == "duplicate":
            headers = [("X-Envai-Contract-Version", "3"), ("X-Envai-Contract-Version", "3")]
        return httpx.Response(
            int(case) if case.isdigit() else 200,
            content=b"bad" if case == "bad_json" else json.dumps(caps).encode(),
            headers=headers,
        )

    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "secret", transport=httpx.MockTransport(handler), retry=RetryPolicy(0)
    )
    try:
        with pytest.raises(CompatibilityError):
            await invoke(c, c.chats.send_message, "chat", body={"message": "hello"})
        assert len(requests) == 1
    finally:
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("case", ["network", "missing_ack", "wrong_ack", "old_route"])
async def test_mutation_once_after_preflight_even_when_outcome_ambiguous(async_, case):
    requests = []

    def handler(req):
        requests.append(req)
        if req.method == "GET":
            return httpx.Response(200, json=CAPABILITIES, headers=ACK)
        assert req.url.path == "/api/v1/atp/v3/chats/chat/messages"
        if case == "network":
            raise httpx.ReadError("ambiguous mutation")
        return httpx.Response(
            404 if case == "old_route" else 202,
            json={"status": "accepted", "turn_id": "cmpl_01ARZ3NDEKTSV4RRFFQ69G5FAV"},
            headers={} if case != "wrong_ack" else {"X-Envai-Contract-Version": "2"},
        )

    c = (AsyncEnvaiClient if async_ else EnvaiClient)("x", transport=httpx.MockTransport(handler))
    try:
        from envai.errors import NotFoundError

        with pytest.raises((TransportError, CompatibilityError, NotFoundError)):
            await invoke(c, c.chats.send_message, "chat", body={"message": "x"})
        assert [r.method for r in requests] == ["GET", "POST"]
    finally:
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
async def test_future_capability_feature_and_immutable_selection(async_):
    caps = {**CAPABILITIES, "features": ["future_feature"]}
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=caps, headers=ACK))
    )
    try:
        result = await invoke(c, c.capabilities.get)
        assert result.features == ["future_feature"]
        with pytest.raises(AttributeError):
            c.contract_version = 2
    finally:
        await close(c)


@pytest.mark.parametrize("version", [1, 4, True, "3", 3.0, None])
def test_bad_contract_selection(version):
    with pytest.raises(ValueError):
        EnvaiClient("x", contract_version=version)


@pytest.mark.parametrize("async_", [False, True])
async def test_legacy_read_only_blocks_before_network(async_):
    def handler(r):
        raise AssertionError("Network must not be reached")

    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", contract_version=2, transport=httpx.MockTransport(handler)
    )
    try:
        for method, args, kwargs in [
            (c.chats.send_message, ["chat"], {"body": {"message": "x"}}),
            (c.wiki.source, ["ws", "file"], {}),
            (c.workspaces.create, [], {"body": {"name": "x"}}),
        ]:
            with pytest.raises(CompatibilityError):
                await invoke(c, method, *args, **kwargs)
    finally:
        await close(c)


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("terminal", ["turn.completed", "turn.cancelled", "turn.failed"])
@pytest.mark.parametrize("nullable", [False, True])
def test_exact_numeric_legacy_and_nullable_v3(valid_events, version, terminal, nullable):
    event = copy.deepcopy(next(e for e in valid_events if e["type"] == "turn.completed"))
    event["v"], event["type"] = version, terminal
    if terminal != "turn.completed":
        event["data"].pop("finish_reason")
        if terminal == "turn.cancelled":
            event["data"]["reason"] = "user"
        else:
            event["data"].update(code="provider_failed", message="safe")
    if nullable:
        event["data"]["cost"] = event["data"]["chat_cost"] = None
        event["data"]["usage"]["cost_usd"] = event["data"]["chat_usage"]["cost_usd"] = None
    if version == 2 and nullable:
        with pytest.raises(ProtocolError):
            parse_event(event)
    else:
        result = parse_event(json.dumps(event))
        assert result.raw == event
        assert result.data.cost == event["data"]["cost"]
        assert result.data.chat_usage.cost_usd == event["data"]["chat_usage"]["cost_usd"]
        if version == 3:
            with pytest.raises(ProtocolError):
                parse_event(event, contract_version=2)


@pytest.mark.parametrize("async_", [False, True])
async def test_mixed_replay_and_pinned_reconnect(async_, valid_events):
    events = copy.deepcopy(valid_events[:3])
    events[2]["v"] = 3
    requests = []

    def handler(req):
        requests.append(req)
        assert req.headers["X-Envai-Contract-Version"] == "3"
        assert req.url.path.startswith("/api/v1/atp/v3/")
        batch = events[:2] if len(requests) == 1 else [events[1], events[2]]
        return httpx.Response(
            200, content=wire(batch), headers={**ACK, "Content-Type": "text/event-stream"}
        )

    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", transport=httpx.MockTransport(handler), retry=RetryPolicy(base_delay=0)
    )
    stream = c.chats.events(events[0]["chat_id"])
    try:
        received = [await anext(stream) if async_ else next(stream) for _ in range(3)]
        assert [e.raw for e in received] == events
        assert requests[1].url.params["after"] == "1"
    finally:
        if async_:
            await stream.aclose()
        else:
            stream.close()
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
async def test_legacy_replay_is_finite(async_, valid_events):
    requests = []

    def handler(req):
        requests.append(req)
        assert req.url.path.startswith("/api/v1/chats/")
        return httpx.Response(
            200,
            content=wire(valid_events[:3]),
            headers={"X-Envai-Contract-Version": "2", "Content-Type": "text/event-stream"},
        )

    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", contract_version=2, transport=httpx.MockTransport(handler)
    )
    stream = c.chats.events(valid_events[0]["chat_id"])
    try:
        result = [e async for e in stream] if async_ else list(stream)
        assert [e.raw for e in result] == valid_events[:3]
        assert len(requests) == 1
    finally:
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("cost", [None, 0, 0.25])
async def test_completion_negotiation_before_hydration(async_, version, cost):
    row = sample(OPENAPI["components"]["schemas"]["Completion"])
    row["cost_usd"] = cost
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(
            200, json={"data": row}, headers={"X-Envai-Contract-Version": str(version)}
        )

    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", contract_version=version, transport=httpx.MockTransport(handler)
    )
    try:
        if version == 2 and cost is None:
            with pytest.raises(ProtocolError):
                await invoke(c, c.completions.get, "ws", "chat", "completion")
        else:
            result = await invoke(c, c.completions.get, "ws", "chat", "completion")
            assert result.cost_usd == cost
        assert seen[0].headers["X-Envai-Contract-Version"] == str(version)
    finally:
        await close(c)


@pytest.mark.parametrize(
    "name",
    [
        "source-accepted",
        "source-conversion-retry",
        "source-conversion-no-audio",
        "source-conversion-exhausted",
        "operation-completed",
        "categories",
        "category-index",
        "usage-pending",
        "usage-empty",
    ],
)
def test_typed_wiki_fixtures_preserve_cost_provenance(name):
    item = json.loads((F / (name + ".json")).read_text())
    value = item["data"]
    validate({"$ref": "#/components/schemas/" + item["schema"]}, value)
    result = getattr(models, item["schema"]).model_validate(value)
    assert result.model_dump(mode="json", by_alias=True, exclude_unset=True) == value


@pytest.mark.parametrize("async_", [False, True])
async def test_upload_streaming_limits_escaping_diagnostics_and_jwks(async_, tmp_path):
    source = fixture("source-accepted")
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPABILITIES, headers=ACK)
        if req.url.path.endswith("wiki-webhook-jwks.json"):
            assert (
                "authorization" not in req.headers and "x-envai-contract-version" not in req.headers
            )
            return httpx.Response(200, json={"keys": []})
        assert "%2F" in str(req.url)
        if req.method == "POST":
            assert req.headers["Idempotency-Key"] == "upload-stable"
            assert b'filename="document.txt"' in req.content and b"hello wiki" in req.content
        return httpx.Response(202, json={"data": source}, headers=ACK)

    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", base_url="https://envai.example/api/v1", transport=httpx.MockTransport(handler)
    )
    path = tmp_path / "document.txt"
    path.write_bytes(b"hello wiki")
    try:
        result = await invoke(c, c.wiki.upload_file, "ws/a", path, idempotency_key="upload-stable")
        assert (
            result.conversion.status == "queued" and result.indexing.status == "pending_conversion"
        )
        assert result.usage_cost.totals is None
        await invoke(c, c.wiki.source, "ws/a", "file/b")
        await invoke(c, c.wiki.jwks)
        assert str(requests[-1].url) == "https://envai.example/.well-known/wiki-webhook-jwks.json"
        with path.open("wb") as f:
            f.truncate(MAX_SOURCE_BYTES + 1)
        with pytest.raises(ValueError):
            await invoke(c, c.wiki.upload_file, "ws", path, idempotency_key="stable")
    finally:
        await close(c)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"from_": 0, "to": 2},
        {"from_": 3, "to": 2},
        {"from_": True, "to": 2},
        {"all": True, "from_": 1},
        {"all": False},
    ],
)
def test_index_invalid_selection(options):
    with pytest.raises(ValueError):
        _index_options(options)


@pytest.mark.parametrize("async_", [False, True])
async def test_exhaustive_catalog_refuses_truncation_and_preserves_revision(async_):
    data = fixture("category-index")
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={"data": data}, headers=ACK)

    c = (AsyncEnvaiClient if async_ else EnvaiClient)("x", transport=httpx.MockTransport(handler))
    try:
        data["is_complete"] = False
        with pytest.raises(ProtocolError):
            await invoke(c, c.wiki.category_index, "ws", "category", all=True, catalog_revision=3)
        assert requests[-1].url.params["catalog_revision"] == "3"
        result = await invoke(c, c.wiki.category_index, "ws", "category", from_=1, to=2)
        assert result.returned_count == len(result.entries)
        with pytest.raises(ValueError):
            await invoke(c, c.get_wiki_category_index, "ws", "category")
    finally:
        await close(c)


def test_raw_upload_validation_and_position(tmp_path):
    for value in [{}, {"files": b"x"}, {"file": object()}, {"file": b"x", "extra": b"y"}]:
        with pytest.raises(ValueError):
            _validate_upload(value)
    file = io.BytesIO(b"text")
    file.seek(2)
    _validate_upload({"file": ("x", file)})
    assert file.tell() == 2
    _validate_upload({"file": b"x"})
    _validate_upload({"file": "x"})
    p = tmp_path / "large"
    with p.open("wb+") as file:
        file.truncate(MAX_SOURCE_BYTES)
        _validate_upload({"file": ("x", file)})
        file.truncate(MAX_SOURCE_BYTES + 1)
        with pytest.raises(ValueError):
            _validate_upload({"file": ("x", file)})


@pytest.mark.parametrize("async_", [False, True])
async def test_cancelled_upload_and_invalid_idempotency_never_preflight(async_):
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", transport=httpx.MockTransport(lambda req: pytest.fail("must not send"))
    )
    try:
        with pytest.raises(ValidationError):
            await invoke(c, c.wiki.upload, "ws", files={"file": b"x"}, idempotency_key="bad key")
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(CancelledError):
            await invoke(
                c,
                c.wiki.upload,
                "ws",
                files={"file": b"x"},
                idempotency_key="key",
                request_options=RequestOptions(cancel=cancel),
            )
    finally:
        await close(c)


def test_new_generator_preserves_conditional_fields_and_inline_requests():
    assert models.UsageCost.model_fields["totals"].is_required()
    assert models.UsageCost.model_fields["revision"].is_required()
    assert models.CreateWikiInboxRunBody(webhook_url="https://receiver.test").webhook_url
    assert models.WithdrawWikiSourceBody(file_id="source").file_id == "source"
    assert len(OPERATIONS) == 71


@pytest.mark.parametrize("async_", [False, True])
async def test_named_legacy_endpoints_cannot_bypass_default_contract(async_):
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", transport=httpx.MockTransport(lambda r: pytest.fail("No network"))
    )
    try:
        with pytest.raises(CompatibilityError):
            await invoke(c, c.get_capabilities_legacy)
        with pytest.raises(CompatibilityError):
            c.stream_chat_events_legacy("chat")
    finally:
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("method", ["get_capabilities", "get_capabilities_legacy"])
async def test_legacy_capability_hydration_is_version_bound(async_, method):
    seen = []
    cap = {
        "protocol_version": 2,
        "families": ["turn"],
        "features": ["replay"],
        "tool_kinds": ["client"],
        "event_types": ["ping"],
    }

    def handler(r):
        seen.append(r)
        return httpx.Response(200, json=cap, headers={"X-Envai-Contract-Version": "2"})

    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", contract_version=2, transport=httpx.MockTransport(handler)
    )
    try:
        result = await invoke(c, getattr(c, method))
        assert isinstance(result, models.CapabilitiesLegacy) and result.protocol_version == 2
        assert seen[-1].url.path == "/api/v1/capabilities"
        cap["protocol_version"] = 3
        with pytest.raises(ProtocolError):
            await invoke(c, getattr(c, method))
    finally:
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("case", ["mixed", "null", "oversize"])
async def test_legacy_snapshot_rejects_entire_tail_before_exposing_prefix(
    async_, case, valid_events
):
    events = copy.deepcopy(valid_events)
    if case == "mixed":
        events[-1]["v"] = 3
    elif case == "null":
        terminal = next(e for e in events if e["type"] == "turn.completed")
        terminal["data"]["cost"] = None
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x",
        contract_version=2,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                content=wire(events),
                headers={"X-Envai-Contract-Version": "2", "Content-Type": "text/event-stream"},
            )
        ),
    )
    stream = c.stream_chat_events_legacy(
        events[0]["chat_id"], max_snapshot_bytes=1 if case == "oversize" else 64 * 1024 * 1024
    )
    try:
        with pytest.raises(ProtocolError):
            await anext(stream) if async_ else next(stream)
        assert stream.last_seq == 0
    finally:
        if async_:
            await stream.aclose()
        else:
            stream.close()
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "fixture_name", ["completed", "recovered", "actual-completed", "actual-recovered"]
)
async def test_completion_snapshot_immutable_current_revision_can_advance(async_, fixture_name):
    snapshot = json.loads((F.parent / "completion-terminal" / (fixture_name + ".json")).read_text())
    row = sample(OPENAPI["components"]["schemas"]["Completion"])
    row.update(
        id=snapshot["data"]["turn_id"],
        terminal_snapshot=snapshot,
        usage_cost=fixture("usage-pending"),
        cost_usd=None,
    )
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": row}, headers=ACK)
        ),
    )
    try:
        first = await invoke(c, c.completions.get, "ws", "chat", row["id"])
        row["usage_cost"] = fixture("usage-empty")
        row["usage_cost"]["revision"] = 100
        row["cost_usd"] = 0
        second = await invoke(c, c.completions.get, "ws", "chat", row["id"])
        assert first.cost_usd is None and second.cost_usd == 0
        assert (
            first.terminal_snapshot.model_dump(mode="json", by_alias=True, exclude_unset=True)
            == snapshot
        )
        assert second.terminal_snapshot == first.terminal_snapshot
        row["id"] = "different"
        with pytest.raises(ProtocolError):
            await invoke(c, c.completions.get, "ws", "chat", row["id"])
    finally:
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
async def test_legacy_completion_page_is_atomic(async_):
    valid = sample(OPENAPI["components"]["schemas"]["Completion"])
    valid["cost_usd"] = 0.25
    invalid = {**valid, "cost_usd": None}
    page = sample(OPENAPI["components"]["schemas"]["CompletionCollection"])
    page["data"] = [valid, invalid]
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x",
        contract_version=2,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=page, headers={"X-Envai-Contract-Version": "2"})
        ),
    )
    try:
        items = c.completions.iterate("ws", "chat")
        with pytest.raises(ProtocolError):
            await anext(items) if async_ else next(items)
    finally:
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
async def test_completion_ack_before_bad_json_and_unsupported_selector(async_):
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"not json"))
    )
    try:
        with pytest.raises(CompatibilityError):
            await invoke(c, c.completions.get, "ws", "chat", "c")
    finally:
        await close(c)
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": {"code": "unsupported_contract_version"}})
        ),
    )
    try:
        with pytest.raises(CompatibilityError):
            await invoke(c, c.completions.get, "ws", "chat", "c")
    finally:
        await close(c)


def test_decimal_original_currency_and_cost_state_constraints():
    value = fixture("usage-pending")
    value["priced_subtotals"] = [
        {"currency": "EUR", "amount": "0.0000000012300"},
        {"currency": "USD", "amount": "99999999999999.123456789"},
    ]
    validate({"$ref": "#/components/schemas/UsageCost"}, value)
    parsed = models.UsageCost.model_validate(value)
    assert [x.amount for x in parsed.priced_subtotals] == [
        x["amount"] for x in value["priced_subtotals"]
    ]
    assert parsed.totals is None
    for update in [
        {"totals": []},
        {"status": "complete"},
        {"status": "complete", "totals": [], "scope_status": "open", "pending_invocation_count": 0},
    ]:
        with pytest.raises(ProtocolError):
            validate({"$ref": "#/components/schemas/UsageCost"}, {**value, **update})


def test_snapshot_limits_and_no_partial_sse_frame(valid_events):
    with EnvaiClient("x", contract_version=2) as c:
        for invalid in (0, -1, True, 1.5):
            with pytest.raises(ValueError):
                c.chats.events(valid_events[0]["chat_id"], max_snapshot_bytes=invalid)
        stream = c.chats.events(valid_events[0]["chat_id"])
        with pytest.raises(ProtocolError):
            stream._snapshot(wire(valid_events[:2]) + b'data: {"v":2}')
        stream.close()


@pytest.mark.parametrize("cost", [-1, float("nan"), float("inf"), float("-inf"), 10**400])
@pytest.mark.parametrize("version", [2, 3])
def test_negative_nonfinite_direct_event_costs_rejected(cost, version, valid_events):
    event = copy.deepcopy(next(e for e in valid_events if e["type"] == "turn.completed"))
    event["v"] = version
    event["data"]["chat_usage"]["cost_usd"] = cost
    with pytest.raises(ProtocolError):
        parse_event(event)


@pytest.mark.parametrize("location", ["cost_usd", "usage", "terminal_snapshot"])
@pytest.mark.parametrize("cost", [-1, None, True, "0.25", 10**400])
def test_legacy_completion_costs_cannot_hide_in_additive_metadata(location, cost):
    row = sample(OPENAPI["components"]["schemas"]["Completion"])
    row["cost_usd"] = 0.25
    if location == "cost_usd":
        row["cost_usd"] = cost
    elif location == "usage":
        row["usage"]["cost_usd"] = cost
    else:
        row["terminal_snapshot"] = {"data": {"cost": cost}}
    with EnvaiClient(
        "x",
        contract_version=2,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"data": row}, headers={"X-Envai-Contract-Version": "2"}
            )
        ),
    ) as c:
        with pytest.raises(ProtocolError):
            c.completions.get("ws", "chat", "completion")


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize(
    "path", [("usage",), ("chat_usage",), ("usage", "input_tokens"), ("cost",), ("chat_cost",)]
)
def test_required_legacy_terminal_fields_remain_required(version, path, valid_events):
    event = copy.deepcopy(next(e for e in valid_events if e["type"] == "turn.completed"))
    event["v"] = version
    target = event["data"]
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    with pytest.raises(ProtocolError):
        parse_event(event)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "name,version", [("source", 3), ("completion-pending", 3), ("legacy-completion", 2)]
)
async def test_actual_control_plane_http_responses(async_, name, version):
    fixture_dir = F.parent / "contract3-http"
    body = (fixture_dir / (name + ".json")).read_bytes()
    if version == 3:
        h = json.loads(
            (
                fixture_dir
                / ("source-headers.json" if name == "source" else "completion-headers.json")
            ).read_text()
        )
        headers = [(key, value) for key, values in h.items() for value in values]
    else:
        headers = {"X-Envai-Contract-Version": "2"}
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x",
        contract_version=version,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body, headers=headers)),
    )
    try:
        row = json.loads(body)["data"]
        if name == "source":
            result = await invoke(c, c.wiki.source, row["workspace_id"], row["file_id"])
            assert result.conversion.error_code == "provider_limit_exceeded"
            assert result.conversion.recoverable and result.conversion.next_retry_at
        else:
            result = await invoke(
                c, c.completions.get, row["workspace_id"], row["chat_id"], row["id"]
            )
            if version == 2:
                assert result.cost_usd == 0.25 and result.usage_cost is None
            else:
                assert result.cost_usd is None and result.usage_cost.totals is None
                assert (
                    result.terminal_snapshot.v == 3
                    and result.terminal_snapshot.accepted_protocol_version == 3
                )
        assert result.model_dump(mode="json", by_alias=True, exclude_unset=True) == row
    finally:
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
async def test_wiki_example_uses_injected_client_without_hidden_work(async_, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wiki_example", Path(__file__).parents[1] / "examples/wiki.py"
    )
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    requests = []

    def handler(r):
        requests.append(r)
        if r.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPABILITIES, headers=ACK)
        data = (
            fixture("operation-completed")
            if r.url.path.endswith("/runs")
            else fixture("source-accepted")
        )
        return httpx.Response(200, json={"data": data}, headers=ACK)

    c = (AsyncEnvaiClient if async_ else EnvaiClient)("x", transport=httpx.MockTransport(handler))
    original = tmp_path / "source.txt"
    original.write_text("synthetic wiki example")
    try:
        result = (
            await example.upload_and_stage_async(c, "ws", original, "example-upload")
            if async_
            else example.upload_and_stage(c, "ws", original, "example-upload")
        )
        assert result.conversion.status == "queued"
        assert len([r for r in requests if r.method == "POST"]) == 1
        if not async_:
            assert example.start_inbox(
                c, "ws", "https://app.example/events", "example-run"
            ).operation_id
    finally:
        await close(c)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("case", ["duplicate", "backwards", "before_cursor", "wrong_chat"])
async def test_legacy_snapshot_rejects_bad_order_atomically(async_, case, valid_events):
    events = copy.deepcopy(valid_events[1:3])
    after = 1 if case == "before_cursor" else 0
    if case == "duplicate":
        events[1]["seq"] = events[0]["seq"]
    elif case == "backwards":
        events.append(copy.deepcopy(events[0]))
    elif case == "wrong_chat":
        events[1]["chat_id"] = "cht_01ARZ3NDEKTSV4RRFFQ69G5FAA"
    c = (AsyncEnvaiClient if async_ else EnvaiClient)(
        "x",
        contract_version=2,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                content=wire(events),
                headers={"X-Envai-Contract-Version": "2", "Content-Type": "text/event-stream"},
            )
        ),
    )
    stream = c.chats.events(events[0]["chat_id"], after=after)
    try:
        with pytest.raises(ProtocolError):
            await anext(stream) if async_ else next(stream)
        assert stream.last_seq == after
    finally:
        if async_:
            await stream.aclose()
        else:
            stream.close()
        await close(c)


@pytest.mark.parametrize("kind", ["agent.done", "agent.tool.complete"])
@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("nullable", [False, True])
def test_remaining_cost_events_preserve_own_agent_tool_attribution(
    kind, version, nullable, valid_events
):
    event = copy.deepcopy(next(e for e in valid_events if e["type"] == "agent.tool.request"))
    usage = copy.deepcopy(
        next(e for e in valid_events if e["type"] == "turn.completed")["data"]["usage"]
    )
    usage["cost_usd"] = None if nullable else 0.25
    event["v"], event["type"] = version, kind
    event["data"] = (
        {"status": "completed", "summary": "done", "usage": usage, "duration_ms": 30}
        if kind == "agent.done"
        else {
            "call_id": event["data"]["call_id"],
            "result": {"ok": True},
            "is_error": False,
            "usage": usage,
        }
    )
    if version == 2 and nullable:
        with pytest.raises(ProtocolError):
            parse_event(event)
    else:
        result = parse_event(event)
        assert result.data.usage.cost_usd == usage["cost_usd"]
        assert result.raw == event
        assert result.agent.instance_id == event["agent"]["instance_id"]
