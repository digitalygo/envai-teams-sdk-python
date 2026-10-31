"""CLI, authenticated adapters, durable locking and validation failure coverage."""
from __future__ import annotations

import json
import multiprocessing
import os
import threading
import uuid
from copy import deepcopy

import httpx
import pytest
from test_definitions_parity import BASE, GOLDEN, Remote

from envai import EnvaiClient
from envai.definitions import (
    ClientRemote,
    DefinitionError,
    Definitions,
    FileStore,
    Manifest,
    MemoryStore,
    PostgresStore,
    SyncOptions,
    compile_manifest,
)
from envai.definitions.cli import main
from envai.definitions.compiler import markdown_agent
from envai.definitions.service import diff


def test_cli_validate_plan_sync_status_state_and_redaction(tmp_path, monkeypatch, capsys):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(BASE))
    state = str(tmp_path / "state.json")
    monkeypatch.delenv("ENVAI_API_KEY", raising=False)
    assert main(["validate", "--path", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"
    assert main(["plan", "--path", str(path), "--state-path", state]) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "KeyError"
    remote = Remote()

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "never-print-secret"
        def list_models(self):
            return {"data": []}
        def list_user_tools(self):
            return remote.list("tool")
        def list_agents(self):
            return []
        def list_teams(self):
            return []
        def create_user_tool(self, *, body):
            return remote.create("tool", body)
        def close(self):
            pass

    monkeypatch.setenv("ENVAI_API_KEY", "never-print-secret")
    for command in ("plan", "sync"):
        assert main([command, "--path", str(path), "--state-path", state, "--dry-run"],
                    client_factory=Client) == 0
        assert "never-print-secret" not in capsys.readouterr().out
    assert remote.calls == []
    assert not (tmp_path / "state.json").exists()
    assert main(["sync", "--path", str(path), "--state-path", state], client_factory=Client) == 0
    assert json.loads(capsys.readouterr().out)["ready"]
    assert main(["status", "--path", str(path), "--state-path", state], client_factory=Client) == 0
    assert json.loads(capsys.readouterr().out)["ready"]
    assert main(["state", "--state-path", state]) == 0
    assert "tool:echo" in json.loads(capsys.readouterr().out)["resources"]
    remote.values["tool"][0]["description"] = "drift"
    assert main(["plan", "--path", str(path), "--state-path", state, "--fail-on-drift"],
                client_factory=Client) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "drift"
    assert main(["sync", "--path", str(path), "--state-path", state], client_factory=Client) == 2
    capsys.readouterr()


def test_authenticated_crud_adapter_no_mutation_retry():
    seen = []
    remote = Remote()

    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer local-test-key"
        path = request.url.path
        kind = "tool" if path.startswith("/api/v1/tools") else \
            "agent" if path.startswith("/api/v1/agents") else "team"
        if request.method == "GET":
            if path.endswith("/models"):
                return httpx.Response(200, json={"object": "list", "data": [],
                                               "defaults": {"primary": "x", "subagent": "x"}})
            return httpx.Response(200, json={"data": remote.list(kind)})
        if request.method == "DELETE":
            remote.delete(kind, path.rsplit("/", 1)[1])
            return httpx.Response(204)
        body = json.loads(request.content)
        result = remote.create(kind, body) if request.method == "POST" else \
            remote.update(kind, path.rsplit("/", 1)[1], body)
        if kind == "tool":
            result["created_at"] = "2026-01-01T00:00:00Z"
            remote.values[kind][0]["created_at"] = result["created_at"]
        return httpx.Response(200 if request.method == "PUT" else 201, json={"data": result})

    with EnvaiClient("local-test-key", transport=httpx.MockTransport(handler)) as client:
        adapter = ClientRemote(client)
        payload = BASE["tools"]["echo"]
        result = adapter.create("tool", payload)
        assert result["name"] == "echo"
        assert adapter.list("tool")[0]["name"] == "echo"
        assert adapter.update("tool", "echo", {**payload, "description": "changed"})["description"] \
            == "changed"
        adapter.delete("tool", "echo")
    assert [request.method for request in seen] == ["POST", "GET", "PUT", "DELETE"]


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(format_version=1),
    lambda d: d.update(unknown=True),
    lambda d: d.update(tools=[]),
    lambda d: d["tools"].update(other=d["tools"]["echo"]),
    lambda d: d["tools"].update(echo=[]),
    lambda d: d["tools"]["echo"].update(name=""),
    lambda d: d["tools"]["echo"].update(kind="server"),
    lambda d: d["tools"]["echo"].update(parameters=[]),
    lambda d: d["tools"]["echo"].update(timeout_seconds=True),
])
def test_invalid_tool_manifests(mutation):
    source = deepcopy(BASE)
    mutation(source)
    with pytest.raises((DefinitionError, TypeError)):
        Manifest(source)


@pytest.mark.parametrize("mutation", [
    lambda d: d["agents"]["worker"].update(system_prompt=""),
    lambda d: d["agents"]["worker"].update(can_write_skills="yes"),
    lambda d: d["agents"]["worker"].update(tools_enabled={}),
    lambda d: d["agents"]["worker"].update(tools_enabled=[{}]),
    lambda d: d["agents"]["worker"].update(external_tools_enabled=[{"name": "missing"}]),
    lambda d: d["teams"]["team"].update(orchestrator_id="worker"),
    lambda d: d["teams"]["team"].update(members={}),
    lambda d: d["teams"]["team"].update(members=[None]),
    lambda d: d["teams"]["team"].update(members=[{"agent_definition_id": "coordinator"}]),
    lambda d: d["teams"]["team"]["members"][0].update(role=3),
])
def test_invalid_agent_team_manifests(mutation):
    source = deepcopy(GOLDEN["dependency_order"]["manifest"])
    mutation(source)
    with pytest.raises(DefinitionError):
        Manifest(source)


def test_compiler_loaders_and_metadata(tmp_path):
    with pytest.raises(DefinitionError):
        compile_manifest(tmp_path / "missing")
    (tmp_path / "manifest.yaml").write_text('format_version: "1"')
    assert compile_manifest(tmp_path).document["tools"] == {}
    (tmp_path / "manifest.json").write_text('{"format_version":"1"}')
    with pytest.raises(DefinitionError):
        compile_manifest(tmp_path)
    (tmp_path / "manifest.json").unlink()
    (tmp_path / "manifest.yaml").unlink()
    agents = tmp_path / "agents"
    agents.mkdir()
    path = agents / "worker.md"
    path.write_text("---\nformat: 1\nmode: subagent\ntools:\n  server: [read, read]\n---\nWork.")
    assert compile_manifest(tmp_path).document["agents"]["worker"]["tools_enabled"] == [{"name": "read"}]
    assert compile_manifest(compile_manifest(tmp_path)).hash == compile_manifest(tmp_path).hash
    for text in ("missing", "---\nformat: 2\n---\nbody", "---\nformat: 1\ntools: []\n---\nbody",
                 "---\nformat: 1\ntools: {server: invalid}\n---\nbody"):
        path.write_text(text)
        with pytest.raises(DefinitionError):
            markdown_agent(path)
    path.unlink()
    (agents / "worker.yaml").write_text('name: Worker\nmode: subagent\nsystem_prompt: Work.')
    (agents / "ignored.txt").write_text("ignored")
    assert compile_manifest(tmp_path).document["agents"]["worker"]["name"] == "Worker"
    (agents / "worker.json").write_text('{}')
    with pytest.raises(DefinitionError):
        compile_manifest(tmp_path)
    (agents / "worker.json").unlink()
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "bad.md").write_text("invalid")
    with pytest.raises(DefinitionError):
        compile_manifest(tmp_path)
    assert diff({"x": 1}, {}) == [{"path": "x", "kind": "removed"}]


def _hold_file(path, acquired, release):
    with FileStore(path).lock("scope"):
        acquired.set()
        release.wait(5)


def test_file_process_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    path = str(tmp_path / "state.json")
    process = context.Process(target=_hold_file, args=(path, acquired, release))
    process.start()
    assert acquired.wait(5)
    entered = threading.Event()
    def second():
        with FileStore(path).lock("scope"):
            entered.set()
    thread = threading.Thread(target=second)
    thread.start()
    assert not entered.wait(.1)
    release.set()
    thread.join(5)
    process.join(5)
    assert entered.is_set() and process.exitcode == 0


def test_postgres_actual_restart_transaction_and_lock():
    dsn = os.getenv("ENVAI_TEST_POSTGRES_DSN") or os.getenv("TEST_DATABASE_URL")
    if not dsn:
        if os.getenv("ENVAI_REQUIRE_POSTGRES") == "1":
            pytest.fail("PostgreSQL DSN required")
        pytest.skip("Set ENVAI_TEST_POSTGRES_DSN for real database tests")
    import psycopg
    scope = "definitions-" + uuid.uuid4().hex
    store = PostgresStore(dsn)
    store.create_schema()
    try:
        with store.lock(scope):
            store.save(scope, "tool:x", {"literal": {"list": [], "object": {}}})
            with pytest.raises(RuntimeError):
                with store.connection.transaction():
                    store.connection.execute("DELETE FROM envai_definition_state WHERE scope=%s", (scope,))
                    raise RuntimeError("rollback")
        with PostgresStore(dsn).lock(scope) as unused:
            assert unused is None
        restarted = PostgresStore(dsn)
        with restarted.lock(scope):
            assert restarted.resources(scope)["tool:x"]["literal"] == {"list": [], "object": {}}
            restarted.delete(scope, "tool:x")
        with pytest.raises(DefinitionError):
            restarted.save(scope, "x", {})
        with psycopg.connect(dsn) as connection:
            connection.execute("UPDATE envai_definition_state SET document='{}' WHERE scope=%s", (scope,))
        with pytest.raises(DefinitionError):
            with restarted.lock(scope):
                pass
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute("DELETE FROM envai_definition_state WHERE scope=%s", (scope,))


def test_safety_overrides_journal_before_effect_and_modes():
    store, remote = MemoryStore(), Remote()
    service = Definitions(remote, store, "scope")
    original = remote.create
    def crash(kind, payload):
        assert store.resources("scope")["tool:echo"]["status"] == "ambiguous"
        raise KeyboardInterrupt("crash")
    remote.create = crash
    with pytest.raises(KeyboardInterrupt):
        service.sync(BASE)
    remote.create = original
    assert service.sync(BASE, SyncOptions(adopt=True, overwrite_drift=True,
                                         recreate_missing=True, prune=True))["status"] == "blocked"
    assert remote.calls == []
    with pytest.raises(DefinitionError):
        service.ensure(BASE, mode="wrong")
    with pytest.raises(DefinitionError):
        service.ensure(BASE, mode="lazy")
    with pytest.raises(DefinitionError):
        SyncOptions(scope="wrong")
    with pytest.raises(DefinitionError):
        Definitions(remote, store, "")
    store.delete("scope", "tool:echo")
    assert service.ensure(BASE, mode="lazy", ttl=0)
    assert not service.ready(BASE, ttl=0)
    remote.values["tool"][0]["description"] = "drift"
    assert service.sync(BASE)["status"] == "blocked"
    assert not service.ready(BASE)


def test_restart_resolves_known_landed_intent_without_mutation():
    remote, store = Remote(), MemoryStore()
    service = Definitions(remote, store, "scope")
    original = remote.create
    def landed_crash(kind, payload):
        original(kind, payload)
        raise KeyboardInterrupt("lost process after committed remote mutation")
    remote.create = landed_crash
    with pytest.raises(KeyboardInterrupt):
        service.sync(BASE)
    remote.create = original
    remote.calls.clear()
    assert service.sync(BASE)["ready"]
    assert remote.calls == []
    original_delete = remote.delete
    def delete_crash(kind, identity):
        original_delete(kind, identity)
        raise KeyboardInterrupt("lost delete acknowledgment")
    remote.delete = delete_crash
    with pytest.raises(KeyboardInterrupt):
        service.sync({"format_version": "1"}, SyncOptions(prune=True))
    remote.calls.clear()
    assert service.sync({"format_version": "1"}, SyncOptions(prune=True))["ready"]
    assert remote.calls == []


def test_catalog_failure_bad_model_and_partial_dependency_failure():
    source = deepcopy(GOLDEN["dependency_order"]["manifest"])
    remote, store = Remote(), MemoryStore()
    service = Definitions(remote, store, "scope")
    source["agents"]["worker"]["model"] = "missing"
    with pytest.raises(DefinitionError):
        service.plan(source)
    source["agents"]["worker"].pop("model")
    assert service.sync(source, SyncOptions(scope="teams"))["status"] == "failed"
    assert remote.calls == []
    assert not service.ready(source)
