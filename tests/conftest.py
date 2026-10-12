from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def vectors():
    return json.loads((FIXTURES / "signatures.json").read_text())


@pytest.fixture
def valid_events():
    value = json.loads((FIXTURES / "events-valid.json").read_text())
    return value


@pytest.fixture
def tool_event(valid_events):
    from envai.events import parse_event

    for item in valid_events:
        event = item.get("event", item)
        if event.get("type") == "agent.tool.request":
            return parse_event(copy.deepcopy(event))
    raise AssertionError("Missing request fixture")
