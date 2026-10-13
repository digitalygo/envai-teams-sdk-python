"""Run the public examples with offline HTTP mocks and no account credentials."""

import copy
import json
import runpy
from pathlib import Path

import httpx
import pytest
from negotiated_transport import negotiated_transport
from test_http import sample
from test_stream import wire

import envai
from envai._schema import OPENAPI


@pytest.mark.parametrize("example", ["quickstart.py", "stream_turn.py", "client_tools.py"])
def test_public_examples_offline(example, valid_events, monkeypatch, tmp_path, capsys):
    events = copy.deepcopy(valid_events[:4] + [valid_events[7]])
    events[-1]["seq"] = 4
    chat_id, turn_id = events[0]["chat_id"], events[1]["turn_id"]
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer example-fixture"
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200, content=wire(events), headers={"content-type": "text/event-stream"}
            )
        if request.url.path.endswith("/messages"):
            assert json.loads(request.content)["message"]
            return httpx.Response(202, json={"status": "accepted", "turn_id": turn_id})
        if request.url.path.endswith("/workspaces"):
            body = sample(OPENAPI["components"]["schemas"]["WorkspaceResource"])
            return httpx.Response(201, json=body)
        if request.url.path.endswith("/chats"):
            body = sample(OPENAPI["components"]["schemas"]["ChatResource"])
            body["data"]["id"] = chat_id
            return httpx.Response(201, json=body)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    original_sync, original_async = envai.EnvaiClient, envai.AsyncEnvaiClient
    monkeypatch.setattr(
        envai,
        "EnvaiClient",
        lambda *args, **kwargs: original_sync(
            *args, **kwargs, transport=negotiated_transport(handler)
        ),
    )
    monkeypatch.setattr(
        envai,
        "AsyncEnvaiClient",
        lambda *args, **kwargs: original_async(
            *args, **kwargs, transport=negotiated_transport(handler)
        ),
    )
    for key, value in {
        "ENVAI_API_KEY": "example-fixture",
        "ENVAI_BASE_URL": "https://example.test",
        "ENVAI_TEAM_ID": "team-example",
        "ENVAI_CHAT_ID": chat_id,
        "ENVAI_ACCOUNT_SCOPE": "example/account",
        "ENVAI_TOOL_STATE_PATH": str(tmp_path / "state.db"),
    }.items():
        monkeypatch.setenv(key, value)
    runpy.run_path(str(Path(__file__).parents[1] / "examples" / example), run_name="__main__")
    assert "turn.completed" in capsys.readouterr().out
    assert sum(path.endswith("/messages") for method, path in requests) == 1
    assert next(i for i, (_, path) in enumerate(requests) if path.endswith("/events")) < next(
        i for i, (_, path) in enumerate(requests) if path.endswith("/messages")
    )
