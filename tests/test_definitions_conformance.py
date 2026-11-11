"""Explicit remaining portable conformance vectors and synchronized lookup facade."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import httpx
import pytest
from test_definitions_parity import BASE, GOLDEN, Remote

from envai import EnvaiClient
from envai.definitions import (
    ClientRemote,
    DefinitionError,
    Definitions,
    Manifest,
    MemoryStore,
    SyncOptions,
    canonical_hash,
    compile_manifest,
)
from envai.errors import AccessDeniedError, ConflictError


def test_equivalent_markdown_json_typed_and_presence_hashes(tmp_path):
    directory = tmp_path / "definitions"
    (directory / "agents").mkdir(parents=True)
    (directory / "agents/primary.md").write_text(
        "---\nformat: 1\nname: Primary\nmode: primary\n---\nWork.\n")
    native = {"format_version": "1", "agents": {"primary": {
        "name": "Primary", "mode": "primary", "system_prompt": "Work."}}}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(native))
    assert compile_manifest(directory).hash == compile_manifest(path).hash == Manifest(native).hash
    variants = []
    for value in ("absent", None, {}, {"literal": []}):
        candidate = deepcopy(native)
        if value != "absent":
            candidate["agents"]["primary"]["config"] = value
        variants.append(Manifest(candidate).hash)
    assert len(set(variants)) == 4
    for value in ("absent", None, []):
        candidate = deepcopy(native)
        if value != "absent":
            candidate["agents"]["primary"]["tools_enabled"] = value
        variants.append(Manifest(candidate).hash)
    assert len(set(variants[-3:])) == 3


def test_jcs_utf16_unicode_literal_shapes_toolsets_and_order():
    # U+1F600 sorts before U+E000 by UTF-16, opposite Python code point order.
    value = {"\ue000": {"literal": []}, "😀": {"literal": {}}}
    expected = '{"😀":{"literal":{}},"\ue000":{"literal":[]}}'.encode()
    assert canonical_hash(value) == "sha256:" + hashlib.sha256(expected).hexdigest()
    source = deepcopy(GOLDEN["dependency_order"]["manifest"])
    source["agents"]["coordinator"]["tools_enabled"] = [{"name": "z"}, {"name": "a"}, {"name": "z"}]
    first = Manifest(source)
    source["agents"]["coordinator"]["tools_enabled"] = [{"name": "a"}, {"name": "z"}]
    assert first.hash == Manifest(source).hash
    source["agents"]["worker2"] = {**source["agents"]["worker"], "name": "Worker2"}
    source["teams"]["team"]["members"].append({"agent_definition_id": "worker2", "role": None})
    before = Manifest(source).hash
    source["teams"]["team"]["members"].reverse()
    assert before != Manifest(source).hash
    tool = deepcopy(BASE)
    tool["tools"]["echo"]["parameters"].update(enum=[{}, []], const={"nested": [[], {}]})
    assert Manifest(tool).document["tools"]["echo"]["parameters"]["enum"] == [{}, []]


def test_team_rename_keeps_identity_and_tool_rename_requires_explicit_prune():
    source = deepcopy(GOLDEN["dependency_order"]["manifest"])
    remote, store = Remote(), MemoryStore()
    service = Definitions(remote, store, "scope")
    assert service.sync(source)["ready"]
    team_id = remote.values["team"][0]["id"]
    source["teams"]["team"]["name"] = "Renamed team"
    assert service.sync(source)["ready"]
    assert remote.values["team"][0]["id"] == team_id
    source["tools"]["new_echo"] = {**source["tools"].pop("echo"), "name": "new_echo"}
    remote.calls.clear()
    assert service.sync(source)["ready"]
    assert {value["name"] for value in remote.values["tool"]} == {"echo", "new_echo"}
    assert not any(call[0] == "delete" for call in remote.calls)
    assert service.sync(source, SyncOptions(prune=True))["ready"]
    assert {value["name"] for value in remote.values["tool"]} == {"new_echo"}


def test_dependency_delete_rejection_preserves_unmanaged_and_stops_pruning():
    remote, store = Remote(), MemoryStore()
    service = Definitions(remote, store, "scope")
    assert service.sync(GOLDEN["dependency_order"]["manifest"])["ready"]
    remote.values["tool"].append({**BASE["tools"]["echo"], "id": "unmanaged", "name": "unmanaged"})
    original = remote.delete
    def reject(kind, identity):
        if kind == "team":
            raise ConflictError("secret user message", http_status=409, type="conflict",
                                code="team_has_chats", request_id="request-1",
                                details={"secret": "must not print"})
        return original(kind, identity)
    remote.delete = reject
    remote.calls.clear()
    result = service.sync({"format_version": "1"}, SyncOptions(prune=True))
    assert result["status"] == "failed" and not result["ready"]
    assert result["outcomes"][0]["error"] == {"http_status": 409, "type": "conflict",
                                               "code": "team_has_chats", "request_id": "request-1"}
    assert "secret" not in json.dumps(result)
    assert remote.calls == []
    assert any(item["id"] == "unmanaged" for item in remote.values["tool"])
    remote.delete = original
    # Explicit operator reconciliation after a definitive rejected delete.
    state = store.resources("scope")["team:team"]
    store.save("scope", "team:team", {**state, "status": "applied"})
    assert service.sync({"format_version": "1"}, SyncOptions(prune=True))["ready"]
    assert [item["id"] for item in remote.values["tool"]] == ["unmanaged"]


def test_synchronized_typed_identity_lookup_never_networks():
    remote, store = Remote(), MemoryStore()
    service = Definitions(remote, store, "scope")
    source = GOLDEN["dependency_order"]["manifest"]
    with pytest.raises(DefinitionError):
        service.require_synchronized(source)
    with pytest.raises(DefinitionError):
        service.tool(source, "echo")
    assert service.sync(source)["ready"]
    remote.catalog_failure = remote.read_failure = True
    service.require_synchronized(source)
    assert service.tool(source, "echo").remote_name == "echo"
    assert service.agent(source, "worker").remote_id == "agt_Worker"
    assert service.team(source, "team").type == "team"
    with pytest.raises(DefinitionError):
        service.resource(source, "invalid", "x")
    with pytest.raises(DefinitionError):
        service.team(source, "missing")
    state = store.resources("scope")["tool:echo"]
    store.save("scope", "tool:echo", {**state, "status": "ambiguous"})
    with pytest.raises(DefinitionError):
        service.tool(source, "echo")


def test_actual_sdk_mutation_auth_rejection_is_not_retried():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"error": {"type": "access_denied", "message": "private"}})
    with EnvaiClient("local-test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AccessDeniedError):
            ClientRemote(client).create("tool", BASE["tools"]["echo"])
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["agent", "team"])
def test_actual_sdk_agent_team_crud_adapter(kind):
    from test_http import sample

    from envai._schema import OPENAPI

    schema_name = kind.title()
    resource = sample(OPENAPI["components"]["schemas"][schema_name])
    payload = sample(OPENAPI["components"]["schemas"][schema_name + "CreateRequest"])
    seen = []
    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer local-test"
        assert "/" + kind + "s" in request.url.path
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [resource]})
        body = json.loads(request.content)
        assert body["name"] == payload["name"]
        return httpx.Response(201 if request.method == "POST" else 200, json={"data": resource})
    with EnvaiClient("local-test", transport=httpx.MockTransport(handler)) as client:
        remote = ClientRemote(client)
        assert remote.create(kind, payload)["id"] == resource["id"]
        assert remote.list(kind)[0]["id"] == resource["id"]
        assert remote.update(kind, resource["id"], payload)["id"] == resource["id"]
        remote.delete(kind, resource["id"])
    assert [request.method for request in seen] == ["POST", "GET", "PUT", "DELETE"]
