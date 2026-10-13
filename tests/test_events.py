import copy
import json
from pathlib import Path

import pytest

from envai._schema import strict_json
from envai.errors import ProtocolError
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


def test_orchestrator_only(valid_events):
    acc = TurnAccumulator()
    for event in valid_events:
        acc.accept(parse_event(event))
    assert acc.text == "hello"
