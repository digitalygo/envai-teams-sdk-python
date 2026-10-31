"""PHP-executed portable golden vectors, plus failure and persistence semantics."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from envai.definitions import (
    DefinitionError,
    Definitions,
    DriftError,
    FileStore,
    Manifest,
    MemoryStore,
    SyncOptions,
    compile_manifest,
)

GOLDEN = json.loads((Path(__file__).parent / "fixtures/definitions/tooling-golden.json").read_text())
BASE = GOLDEN["hash_vectors"][0]["document"]
MARKER = "2026-01-01T00:00:00Z"


class Remote:
    def __init__(self):
        self.values = {"tool": [], "agent": [], "team": []}
        self.calls = []
        self.fail = None
        self.landed = False
        self.read_failure = False
        self.catalog_failure = False

    def catalog(self):
        if self.catalog_failure:
            raise ConnectionError("secret")
        return ["test-model"]

    def list(self, kind):
        if self.read_failure:
            raise ConnectionError("secret")
        return deepcopy(self.values[kind])

    def create(self, kind, payload):
        self.calls.append(("create", kind, deepcopy(payload)))
        result = {**deepcopy(payload), "id": {"tool": "twl_", "agent": "agt_", "team": "tem_"}[kind]
                  + payload["name"], "updated_at": MARKER}
        if self.fail == kind and not self.landed:
            raise TimeoutError("secret")
        self.values[kind].append(result)
        if self.fail == kind:
            raise TimeoutError("secret")
        return deepcopy(result)

    def update(self, kind, identity, payload):
        self.calls.append(("update", kind, deepcopy(payload)))
        item = next(item for item in self.values[kind] if
                    item["name" if kind == "tool" else "id"] == identity)
        item.update(deepcopy(payload))
        if self.fail == kind:
            raise TimeoutError("secret")
        return deepcopy(item)

    def delete(self, kind, identity):
        self.calls.append(("delete", kind, None))
        self.values[kind] = [item for item in self.values[kind] if
                             item["name" if kind == "tool" else "id"] != identity]
        if self.fail == kind:
            raise TimeoutError("secret")


@pytest.mark.parametrize("case", GOLDEN["planner_reconciler_cases"], ids=lambda c: c["id"])
def test_php_golden(case):
    remote, store = Remote(), MemoryStore()
    source = {"format_version": "1", "tools": {"echo": case["desired"]}
              if case["desired"] else {}, "agents": {}, "teams": {}}
    if case["remote"]:
        remote.values["tool"] = [{**case["remote"], "id": "twl_echo", "updated_at": MARKER}]
    if case["last_applied"]:
        store.save("scope", "tool:echo", {"remote_id": "twl_echo", "remote_name": "echo",
                   "last_applied": case["last_applied"], "remote_marker": MARKER, "status": "applied"})
    renames = {"overwriteDrift": "overwrite_drift", "recreateMissing": "recreate_missing",
               "dryRun": "dry_run", "failOnDrift": "fail_on_drift"}
    options = SyncOptions(**{renames.get(k, k): v for k, v in (case["options"] or {}).items()})
    service = Definitions(remote, store, "scope")
    plan = service.plan(source, options)
    assert plan == service.plan(source, options)
    assert plan["operations"] == case["expected"]["operations"]
    result = service.sync(source, options)
    assert result["status"] == case["expected"]["report_status"]
    assert result["ready"] == (False if case["id"] == "adopt_different"
                               else case["expected"]["manifest_ready"])
    assert [v["status"] for v in result["outcomes"]] == case["expected"]["outcomes"]
    expected_calls = case["expected"]["mutation_calls"]
    assert len(remote.calls) == len(expected_calls)
    for call, expected in zip(remote.calls, expected_calls):
        assert call[0] + "Tool" == expected["method"]
        assert call[2] == expected["payload"]
    stored = store.resources("scope").get("tool:echo")
    expected = case["expected"]["stored"]
    assert stored is None if expected is None else all(stored[k] == v for k, v in expected.items())


def test_hashes_dependency_ids_and_zero_write_reapply():
    for vector in GOLDEN["hash_vectors"]:
        assert Manifest(vector["document"]).hash == vector["sha256"]
    source = GOLDEN["dependency_order"]["manifest"]
    remote, store = Remote(), MemoryStore()
    service = Definitions(remote, store, "scope")
    result = service.sync(source)
    assert result["ready"]
    assert [c[1] for c in remote.calls] == ["tool", "agent", "agent", "team"]
    assert remote.calls[-1][2]["orchestrator_id"] == "agt_Coordinator"
    assert remote.calls[-1][2]["members"][0]["agent_definition_id"] == "agt_Worker"
    remote.calls.clear()
    assert service.sync(source)["ready"]
    assert remote.calls == []
    assert service.ready(source)
    remote.catalog_failure = True
    assert service.ready(source)
    assert service.ensure(source, mode="manual")
    assert service.ensure(source, mode="lazy")
    with pytest.raises(ConnectionError):
        service.plan(source)


@pytest.mark.parametrize("landed", [False, True])
def test_uncertain_create_only_one_mutation(landed):
    remote, store = Remote(), MemoryStore()
    remote.fail, remote.landed = "tool", landed
    result = Definitions(remote, store, "scope").sync(BASE)
    assert len(remote.calls) == 1
    assert result["status"] == ("succeeded" if landed else "ambiguous")
    assert result["ready"] is landed


def test_partial_failure_and_reverse_prune():
    remote, store = Remote(), MemoryStore()
    service = Definitions(remote, store, "scope")
    remote.fail = "agent"
    result = service.sync(GOLDEN["dependency_order"]["manifest"])
    assert result["status"] == "ambiguous"
    assert list(store.resources("scope")) == ["tool:echo", "agent:coordinator"]
    assert store.resources("scope")["agent:coordinator"]["status"] == "ambiguous"
    assert service.sync(GOLDEN["dependency_order"]["manifest"])["status"] == "blocked"
    # Explicit operator reconciliation of known absent remote resource.
    store.delete("scope", "agent:coordinator")
    assert [out["status"] for out in result["outcomes"]] == [
        "succeeded", "ambiguous", "skipped", "skipped"]
    remote.fail = None
    assert service.sync(GOLDEN["dependency_order"]["manifest"])["ready"]
    remote.calls.clear()
    result = service.sync({"format_version": "1"}, SyncOptions(prune=True))
    assert result["ready"]
    assert [call[1] for call in remote.calls] == ["team", "agent", "agent", "tool"]
    assert store.resources("scope") == {}


def test_rename_drift_fail_closed_and_delete_timeout():
    remote, store = Remote(), MemoryStore()
    source = deepcopy(GOLDEN["dependency_order"]["manifest"])
    service = Definitions(remote, store, "scope")
    service.sync(source)
    source["agents"]["worker"]["name"] = "Renamed"
    assert service.sync(source)["ready"]
    assert store.resources("scope")["agent:worker"]["remote_id"] == "agt_Worker"
    remote.values["tool"][0]["description"] = "remote drift"
    with pytest.raises(DriftError) as error:
        service.plan(source, SyncOptions(fail_on_drift=True))
    assert error.value.plan["operations"][0]["classification"] == "drift"
    remote.fail = "tool"
    assert service.sync({"format_version": "1"}, SyncOptions(prune=True))["ready"]


def test_file_restart_corruption_and_scopes(tmp_path):
    path = tmp_path / "nested/state.json"
    service = Definitions(Remote(), FileStore(path), "stable-account")
    assert service.sync(BASE)["ready"]
    restarted = Definitions(Remote(), FileStore(path), "stable-account")
    assert restarted.ready(BASE)
    assert not Definitions(Remote(), FileStore(path), "another-account").ready(BASE)
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text('{"format_version":"999","scopes":{}}')
    with pytest.raises(DefinitionError):
        restarted.ready(BASE)
    path.write_text("not json")
    with pytest.raises(DefinitionError):
        restarted.ready(BASE)
    with pytest.raises(DefinitionError):
        FileStore(path).save("scope", "tool:x", {})


def test_compile_yaml_markdown_literal_shapes_and_validation(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "worker.md").write_bytes(
        b"---\r\nformat: 1\r\nmode: subagent\r\n---\r\n Work. \r\n")
    manifest = compile_manifest(tmp_path)
    assert manifest.document["agents"]["worker"]["system_prompt"] == "Work."
    assert manifest.document["agents"]["worker"]["name"] == "worker"
    source = deepcopy(BASE)
    source["tools"]["echo"]["parameters"]["properties"]["text"]["default"] = {}
    first = Manifest(source).hash
    source["tools"]["echo"]["parameters"]["properties"]["text"]["default"] = []
    assert Manifest(source).hash != first
    for value in (None, "", "wrong"):
        bad = deepcopy(manifest.document)
        bad["agents"]["worker"]["mode"] = value
        with pytest.raises(DefinitionError):
            Manifest(bad)
    bad = deepcopy(manifest.document)
    bad["agents"]["worker"]["config"] = {"nested": [{"max_tokens": 1}]}
    with pytest.raises(DefinitionError):
        Manifest(bad)
    path = tmp_path / "duplicate.json"
    path.write_text('{"format_version":"1","format_version":"1"}')
    with pytest.raises(DefinitionError):
        compile_manifest(path)
    path = tmp_path / "duplicate.yaml"
    path.write_text('format_version: "1"\nformat_version: "1"')
    with pytest.raises(DefinitionError):
        compile_manifest(path)
