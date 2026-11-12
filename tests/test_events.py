import copy
import hashlib
import json
from pathlib import Path

import pytest

from envai import models
from envai._schema import LEGACY_EVENTS, strict_json
from envai.errors import ProtocolError
from envai.event_models import (
    EVENT_TYPES,
    BillingCredit_ExhaustedEvent,
    BillingLimit_ReachedEvent,
    ChatCronWakeupEvent,
    ChatTitleUpdatedEvent,
)
from envai.events import SSEDecoder, TurnAccumulator, UnknownEvent, parse_event

F = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("event", json.loads((F / "events-valid.json").read_text()))
def test_shared_valid(event):
    parsed = parse_event(json.dumps(event, ensure_ascii=False))
    assert parsed.raw == event
    assert parsed.seq == event["seq"]


@pytest.mark.parametrize(
    "case", json.loads((F / "events-invalid.json").read_text()), ids=lambda c: c["name"]
)
def test_shared_invalid(case):
    with pytest.raises(ProtocolError) as error:
        parse_event(case["event"], contract_version=2)
    assert error.value.raw == case["event"]


@pytest.mark.parametrize(
    "case", json.loads((F / "sse-cases.json").read_text()), ids=lambda c: c["name"]
)
def test_shared_sse_all_splits(case):
    wire = case["wire"].encode()
    for split in range(len(wire) + 1):
        parser = SSEDecoder()
        assert parser.feed(wire[:split]) + parser.feed(wire[split:]) == case["data"]
        parser.finish()
    parser = SSEDecoder()
    assert sum((parser.feed(bytes([b])) for b in wire), []) == case["data"]


def test_sse_edge_cases():
    parser = SSEDecoder()
    assert parser.feed(b"data\r\rdata: abc\r\r") == ["", "abc"]
    with pytest.raises(ProtocolError):
        SSEDecoder(3).feed(b"data: big")
    with pytest.raises(ProtocolError):
        SSEDecoder().feed(b"\xff")
    parser = SSEDecoder()
    parser.feed(b"\xc3")
    with pytest.raises(ProtocolError):
        parser.finish()
    with pytest.raises(ValueError):
        SSEDecoder(0)


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_strict_json(text):
    with pytest.raises(ValueError):
        strict_json(text)


@pytest.mark.parametrize("text", ["[DONE]", "[]", "{}", '{"v":2}', '{"v":2,"v":3}'])
def test_invalid_json(text):
    with pytest.raises(ProtocolError):
        parse_event(text)


def test_unknown_payload_preserved(valid_events):
    event = copy.deepcopy(valid_events[1])
    event["type"] = "future.widget"
    event["data"] = {"nested": {"x": [1, None]}}
    assert isinstance(parse_event(event), UnknownEvent)
    for timestamp in ("2026-10-12", "2026-10-12T00:00:00", "2026-02-30T00:00:00Z"):
        event["ts"] = timestamp
        with pytest.raises(ProtocolError):
            parse_event(event)


TYPED_EVENTS = {
    "chat.title.updated": (
        ChatTitleUpdatedEvent,
        models.ATPChatTitleUpdated,
        {"title": "Q3 rollout plan"},
        "cmpl_01ARZ3NDEKTSV4RRFFQ69G5FAV",
    ),
    "chat.cron.wakeup": (
        ChatCronWakeupEvent,
        models.ATPChatCronWakeup,
        {
            "cron_id": "cron_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "occurrence_id": "occ_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "scheduled_at": "2026-10-12T22:00:00Z",
            "delivered_at": "2026-10-12T22:00:01Z",
            "prompt": "Check the rollout",
            "target_mode": "same_chat",
            "dispatch_mode": "turn_injected",
        },
        "cmpl_01ARZ3NDEKTSV4RRFFQ69G5FAV",
    ),
    "billing.limit_reached": (
        BillingLimit_ReachedEvent,
        models.ATPBillingLimit_Reached,
        {
            "reason": "limit_reached",
            "scope": "project",
            "currency": "EUR",
            "reset_at": None,
            "amount": "0.100000000000",
            "remaining": "0",
        },
        None,
    ),
    "billing.credit_exhausted": (
        BillingCredit_ExhaustedEvent,
        models.ATPBillingCredit_Exhausted,
        {"reason": "credit_exhausted", "scope": "company", "currency": "EUR"},
        None,
    ),
}


def typed_envelope(event_type, payload, turn_id, version=3):
    return {
        "v": version,
        "id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        "seq": 11,
        "ts": "2026-10-12T22:00:00Z",
        "chat_id": "cht_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "turn_id": turn_id,
        "agent": None,
        "type": event_type,
        "data": payload,
    }


@pytest.mark.parametrize("event_type", sorted(TYPED_EVENTS))
def test_current_server_events_are_typed_and_nonterminal(event_type):
    event_class, payload_class, payload, turn_id = TYPED_EVENTS[event_type]
    event = typed_envelope(event_type, payload, turn_id)
    parsed = parse_event(event)
    assert EVENT_TYPES[event_type] is event_class
    assert isinstance(parsed, event_class) and isinstance(parsed.data, payload_class)
    assert parsed.turn_id == turn_id and parsed.raw == event and not parsed.terminal
    accumulator = TurnAccumulator()
    accumulator.accept(parsed)
    assert accumulator.text == ""


@pytest.mark.parametrize("event_type", ["billing.limit_reached", "billing.credit_exhausted"])
def test_billing_notices_accept_null_turn_before_admission(event_type):
    _, _, payload, _ = TYPED_EVENTS[event_type]
    parsed = parse_event(typed_envelope(event_type, payload, None))
    assert parsed.turn_id is None and not parsed.terminal
    assert parsed.raw["data"] == payload


def test_frozen_v2_event_catalog_is_unchanged():
    raw = (Path(__file__).parents[1] / "src/envai/_contracts/events-v2.json").read_bytes()
    provenance = json.loads((F / "contract3-provenance.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == provenance["sources"]["events-v2.json"]["sha256"]
    title_class, title_payload_class, title_payload, title_turn = TYPED_EVENTS["chat.title.updated"]
    assert "chat.title.updated" in LEGACY_EVENTS["$defs"]
    legacy_title = parse_event(
        typed_envelope("chat.title.updated", title_payload, title_turn, version=2),
        contract_version=2,
    )
    assert isinstance(legacy_title, title_class)
    assert isinstance(legacy_title.data, title_payload_class)
    for event_type in (
        "chat.cron.wakeup",
        "billing.limit_reached",
        "billing.credit_exhausted",
    ):
        assert event_type not in LEGACY_EVENTS["$defs"]


def test_orchestrator_only(valid_events):
    acc = TurnAccumulator()
    for event in valid_events:
        acc.accept(parse_event(event))
    assert acc.text == "hello"
