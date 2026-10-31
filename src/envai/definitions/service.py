"""Conservative three-way reconciliation through the normal authenticated SDK client."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from envai.errors import EnvaiError, NotFoundError, ServerError, TransportError

from .compiler import DefinitionError, Manifest, canonical_hash, compile_manifest
from .stores import MemoryStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_unset=True)
    if isinstance(value, list):
        return [_dump(item) for item in value]
    return value


class Remote(Protocol):
    def catalog(self) -> list[str]: ...
    def list(self, kind: str) -> list[dict[str, Any]]: ...
    def create(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def update(self, kind: str, identity: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def delete(self, kind: str, identity: str) -> None: ...


class ClientRemote:
    """Uses public CRUD methods, preserving SDK auth, timeouts and error handling."""

    def __init__(self, client: Any):
        self.client = client

    def catalog(self) -> list[str]:
        result = _dump(self.client.list_models())
        if isinstance(result, dict):
            result = result.get("data", result.get("models", []))
        return [item["id"] for item in result]

    def list(self, kind: str) -> list[dict[str, Any]]:
        result = _dump(getattr(self.client, "list_" + ("user_tools" if kind == "tool"
                                                     else kind + "s"))())
        if isinstance(result, dict):
            result = result.get("data", [])
        return result

    def create(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _dump(getattr(self.client, "create_" + ("user_tool" if kind == "tool"
                                                      else kind))(body=payload))

    def update(self, kind: str, identity: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "tool":
            payload = {key: value for key, value in payload.items() if key not in ("name", "kind")}
        return _dump(getattr(self.client, "update_" + ("user_tool" if kind == "tool"
                                                      else kind))(identity, body=payload))

    def delete(self, kind: str, identity: str) -> None:
        getattr(self.client, "delete_" + ("user_tool" if kind == "tool" else kind))(identity)


def normalize(kind: str, payload: dict[str, Any], agents: dict[str, str]) -> dict[str, Any]:
    value = deepcopy(payload)
    for field in ("id", "user_id", "created_at", "updated_at"):
        value.pop(field, None)
    if value.get("description") is None:
        value.pop("description", None)
    if kind == "agent":
        for field in ("model", "tools_enabled", "external_tools_enabled", "permissions", "config"):
            if value.get(field) in (None, [], {}):
                value.pop(field, None)
        for field in ("tools_enabled", "external_tools_enabled"):
            if field in value:
                value[field] = [{"name": name} for name in
                                sorted({item["name"] for item in value[field]})]
    if kind == "team":
        value["orchestrator_id"] = agents.get(value.get("orchestrator_id", ""),
                                               value.get("orchestrator_id"))
        for member in value.get("members", []):
            member["agent_definition_id"] = agents.get(member["agent_definition_id"],
                                                       member["agent_definition_id"])
            member.pop("name", None)
            member.pop("position", None)
    return value


def diff(before: Any, after: Any, prefix: str = "") -> list[dict[str, Any]]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in dict.fromkeys([*before, *after]):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before:
                changes.append({"path": path, "kind": "added"})
            elif key not in after:
                changes.append({"path": path, "kind": "removed"})
            else:
                changes.extend(diff(before[key], after[key], path))
        return changes
    return [{"path": prefix, "kind": "changed"}]


@dataclass(frozen=True)
class SyncOptions:
    adopt: bool = False
    overwrite_drift: bool = False
    recreate_missing: bool = False
    prune: bool = False
    fail_on_drift: bool = False
    dry_run: bool = False
    scope: str = "all"

    def __post_init__(self) -> None:
        if self.scope not in ("all", "tools", "agents", "teams"):
            raise DefinitionError("Invalid reconciliation scope")


@dataclass(frozen=True)
class ResourceIdentity:
    """A ready deployment identity; remote name remains distinct from stable ID."""

    type: str
    local_key: str
    remote_id: str
    remote_name: str


class DriftError(DefinitionError):
    def __init__(self, plan: dict[str, Any]):
        super().__init__("Definition drift requires review")
        self.plan = plan


class Definitions:
    """Compile, inspect, reconcile and check deployment readiness.

    Scope is a stable account/deployment identity, never an API key. Public plan and
    sync operations each hold one exclusive lock across catalog and remote reads.
    """

    def __init__(self, remote: Remote, store: MemoryStore, scope: str):
        if not scope or not scope.strip():
            raise DefinitionError("A stable state scope is required")
        self.remote, self.store, self.scope = remote, store, scope

    def _plan(self, manifest: Manifest, options: SyncOptions) -> dict[str, Any]:
        catalog = self.remote.catalog()  # Fail closed even when no models are explicitly selected.
        for agent in manifest.document["agents"].values():
            if agent.get("model") is not None and agent["model"] not in catalog:
                raise DefinitionError("Agent model is absent from the remote catalog")
        stored = self.store.resources(self.scope)
        reverse = {value["remote_id"]: key.split(":", 1)[1] for key, value in stored.items()
                   if key.startswith("agent:") and value.get("remote_id")}
        operations = []
        for kind in ("tool", "agent", "team"):
            if options.scope not in ("all", kind + "s"):
                continue
            remote = self.remote.list(kind)
            desired = manifest.document[kind + "s"]
            keys = list(desired) + [key.split(":", 1)[1] for key in stored
                                  if key.startswith(kind + ":")
                                  and key.split(":", 1)[1] not in desired]
            for key in keys:
                identifier = kind + ":" + key
                state = stored.get(identifier)
                wanted = desired.get(key)
                identity = state.get("remote_id") if state else None
                name = wanted["name"] if wanted else (state or {})["remote_name"]
                recover_create = bool(state and state.get("status") == "ambiguous"
                    and state.get("intended_action") in ("create", "recreate") and not identity)
                candidates = [item for item in remote if
                    (item.get("id") == identity if state and kind != "tool" and not recover_create
                     else item.get("name") == (state["remote_name"] if state else name))]
                if len(candidates) > 1:
                    raise DefinitionError("Remote identity is ambiguous")
                found = candidates[0] if candidates else None
                actual = normalize(kind, found, reverse) if found is not None else None
                target = normalize(kind, wanted, {}) if wanted is not None else None
                previous = state.get("last_applied") if state else None
                action, blocking, reason = "none", False, None
                if state and state.get("status") == "ambiguous":
                    classification, blocking, reason = "ambiguous", True, "ambiguous"
                    intended = state.get("intended")
                    if state.get("intended_action") == "delete" and found is None:
                        classification, action, blocking, reason = "recovered", "forget", False, None
                    elif intended is not None and found is not None and actual == normalize(
                        kind, intended, reverse
                    ) and target == actual:
                        classification, action, blocking, reason = "recovered", "recover", False, None
                elif wanted is None:
                    classification = "orphan_managed"
                    action = "delete" if options.prune else "none"
                    reason = None if options.prune else "prune_disabled"
                elif state is None and found is None:
                    classification, action = "create", "create"
                elif state is None:
                    classification = "adoption_required"
                    if options.adopt:
                        action = "adopt"
                    else:
                        blocking, reason = True, "adoption_required"
                elif found is None:
                    classification = "missing_remote"
                    if options.recreate_missing:
                        action = "recreate"
                    else:
                        blocking, reason = True, "missing_remote"
                elif target == previous and actual == previous:
                    classification = "unchanged"
                elif actual == previous:
                    classification, action = "update", "update"
                elif target == previous:
                    classification = "drift"
                    if options.overwrite_drift:
                        action = "update"
                    else:
                        blocking, reason = True, "drift"
                else:
                    classification, blocking, reason = "conflict", True, "conflict"
                operations.append({
                    "id": identifier, "type": kind, "local_key": key,
                    "classification": classification, "action": action,
                    "blocking": blocking, "reason_code": reason,
                    "remote_id": found.get("id") if found else identity,
                    "remote_name": found.get("name") if found else (state or {}).get("remote_name"),
                    "remote_marker": (state.get("remote_marker") if state else None)
                    or (found.get("updated_at") if found else None),
                    "destructive": action == "delete", "changes": diff(
                        (previous if classification == "missing_remote" else actual) or {}, target or {}),
                })
        plan = {"format_version": "1", "manifest_hash": manifest.hash,
                "options": asdict(options), "operations": operations}
        plan["hash"] = canonical_hash(plan)
        if options.fail_on_drift and any(op["classification"] in
                                        ("drift", "conflict", "missing_remote") for op in operations):
            raise DriftError(plan)
        return plan

    def plan(self, source: Any, options: SyncOptions | None = None) -> dict[str, Any]:
        with self.store.lock(self.scope):
            return self._plan(compile_manifest(source), options or SyncOptions())

    def ready(self, source: Any, ttl: float | None = None) -> bool:
        manifest = compile_manifest(source)
        with self.store.lock(self.scope):
            ready = self.store.ready(self.scope)
            if not ready or ready["manifest_hash"] != manifest.hash:
                return False
            return ttl is None or (datetime.now(timezone.utc) -
                                   datetime.fromisoformat(ready["timestamp"])).total_seconds() < ttl

    def require_synchronized(self, source: Any) -> None:
        """Fail closed without remote calls unless this exact manifest is ready."""
        if not self.ready(source):
            raise DefinitionError("Definitions are not synchronized")

    def resource(self, source: Any, kind: str, key: str) -> ResourceIdentity:
        """Read a typed identity under the same lock as the readiness check."""
        if kind not in ("tool", "agent", "team"):
            raise DefinitionError("Unknown definition resource type")
        manifest = compile_manifest(source)
        with self.store.lock(self.scope):
            ready = self.store.ready(self.scope)
            if not ready or ready.get("manifest_hash") != manifest.hash:
                raise DefinitionError("Definitions are not synchronized")
            if key not in manifest.document[kind + "s"]:
                raise DefinitionError("Unknown definition local key")
            state = self.store.resources(self.scope).get(kind + ":" + key)
            if not state or state.get("status") != "applied" or not state.get("remote_id"):
                raise DefinitionError("Definition has no synchronized identity")
            return ResourceIdentity(kind, key, state["remote_id"], state["remote_name"])

    def tool(self, source: Any, key: str) -> ResourceIdentity:
        return self.resource(source, "tool", key)

    def agent(self, source: Any, key: str) -> ResourceIdentity:
        return self.resource(source, "agent", key)

    def team(self, source: Any, key: str) -> ResourceIdentity:
        return self.resource(source, "team", key)

    def ensure(self, source: Any, *, mode: str = "deploy", ttl: float = 300) -> bool:
        if mode not in ("deploy", "manual", "off", "lazy"):
            raise DefinitionError("Invalid definition mode")
        if mode != "lazy":
            return self.ready(source)
        with self.store.lock(self.scope):
            manifest = compile_manifest(source)
            ready = self.store.ready(self.scope)
            if ready and ready["manifest_hash"] == manifest.hash and (
                datetime.now(timezone.utc) - datetime.fromisoformat(ready["timestamp"])
            ).total_seconds() < ttl:
                return True
            report = self._sync(manifest, SyncOptions())
            if report["status"] != "succeeded":
                raise DefinitionError("Lazy definition reconciliation did not succeed")
            return report["ready"]

    def sync(self, source: Any, options: SyncOptions | None = None) -> dict[str, Any]:
        with self.store.lock(self.scope):
            return self._sync(compile_manifest(source), options or SyncOptions())

    def _sync(self, manifest: Manifest, options: SyncOptions) -> dict[str, Any]:
        # Real synchronization invalidates readiness before any remote preparation.
        # Catalog/list/identity failures must not leave a previous success usable.
        if not options.dry_run:
            self.store.mark_ready(self.scope, None)
        plan = self._plan(manifest, options)
        blocked = any(op["blocking"] for op in plan["operations"])
        report: dict[str, Any] = {"format_version": "1", "plan": plan,
                                 "status": "dry_run" if options.dry_run else
                                 "blocked" if blocked else "succeeded", "ready": False,
                                 "outcomes": []}
        if options.dry_run or blocked:
            if blocked and not options.dry_run:
                self.store.mark_ready(self.scope, None)
            report["outcomes"] = [{"id": op["id"], "status": "pending" if options.dry_run
                                   else "skipped"} for op in plan["operations"]]
            return report
        # Invalidate any older ready marker before the first mutation.
        self.store.mark_ready(self.scope, None)
        matches_desired = True
        operations = sorted(plan["operations"], key=lambda op:
                            (1, -{"tool": 0, "agent": 1, "team": 2}[op["type"]])
                            if op["action"] in ("delete", "forget") else
                            (0, {"tool": 0, "agent": 1, "team": 2}[op["type"]]))
        for op in operations:
            outcome: dict[str, Any] = {"id": op["id"], "status": "skipped"}
            report["outcomes"].append(outcome)
            if report["status"] != "succeeded" or op["action"] == "none":
                continue
            kind, key, action = op["type"], op["local_key"], op["action"]
            states = self.store.resources(self.scope)
            desired = manifest.document[kind + "s"].get(key)
            payload = deepcopy(desired)
            identity = op["remote_name"] if kind == "tool" else op["remote_id"]
            def journal() -> None:
                previous = self.store.resources(self.scope).get(op["id"], {})
                self.store.save(self.scope, op["id"], {
                    **previous, "type": kind, "local_key": key,
                    "remote_id": op["remote_id"], "remote_name": op["remote_name"]
                    or (payload or {}).get("name"), "status": "ambiguous",
                    "last_applied": previous.get("last_applied"),
                    "intended": payload, "intended_action": action, "timestamp": _now(),
                })
            try:
                if kind == "team" and payload:
                    payload["orchestrator_id"] = states["agent:" + payload["orchestrator_id"]]["remote_id"]
                    for member in payload["members"]:
                        member["agent_definition_id"] = states[
                            "agent:" + member["agent_definition_id"]]["remote_id"]
                if action == "forget":
                    self.store.delete(self.scope, op["id"])
                    outcome["status"] = "succeeded"
                    continue
                if action in ("adopt", "recover"):
                    result = self._find(kind, op["remote_id"], op["remote_name"])
                    if result is None:
                        raise DefinitionError("Adoption candidate disappeared")
                    self._save(manifest, op, result)
                    if action == "adopt" and options.overwrite_drift and normalize(kind, result, self._reverse()) != \
                            normalize(kind, desired, {}):
                        journal()
                        result = self._mutate(kind, "update", identity, payload)
                    elif normalize(kind, result, self._reverse()) != normalize(kind, desired, {}):
                        matches_desired = False
                else:
                    journal()
                    result = self._mutate(kind, action, identity, payload)
                if action == "delete":
                    self.store.delete(self.scope, op["id"])
                else:
                    self._save(manifest, op, result)
                outcome["status"] = "succeeded"
            except AmbiguousMutation:
                outcome["status"] = report["status"] = "ambiguous"
            except Exception as exc:
                outcome["status"] = report["status"] = "failed"
                # Exception messages may contain credentials or remote user data.
                outcome["error_type"] = type(exc).__name__
                if isinstance(exc, EnvaiError):
                    outcome["error"] = {"http_status": exc.http_status, "type": exc.type,
                                        "code": exc.code, "request_id": exc.request_id}
        if report["status"] == "succeeded" and options.scope == "all" and matches_desired:
            self.store.mark_ready(self.scope, {"manifest_hash": manifest.hash, "timestamp": _now()})
            report["ready"] = True
        return report

    def _reverse(self) -> dict[str, str]:
        return {value["remote_id"]: key.split(":", 1)[1]
                for key, value in self.store.resources(self.scope).items()
                if key.startswith("agent:") and value.get("remote_id")}

    def _find(self, kind: str, identity: str | None, name: str) -> dict[str, Any] | None:
        candidates = [item for item in self.remote.list(kind) if
                      (item.get("id") == identity if identity and kind != "tool"
                       else item.get("name") == name)]
        if len(candidates) > 1:
            raise DefinitionError("Remote identity is ambiguous")
        return candidates[0] if candidates else None

    def _mutate(self, kind: str, action: str, identity: str,
                payload: dict[str, Any] | None) -> dict[str, Any]:
        try:
            if action == "delete":
                try:
                    self.remote.delete(kind, identity)
                except NotFoundError:
                    pass
                return {}
            if action in ("create", "recreate"):
                return self.remote.create(kind, payload or {})
            return self.remote.update(kind, identity, payload or {})
        except (TransportError, ServerError, TimeoutError, ConnectionError) as exc:
            try:
                result = self._find(kind, None if action in ("create", "recreate") else identity,
                                    payload["name"] if payload else identity)
                if action == "delete" and result is None:
                    return {}
                if result is not None and payload is not None and \
                        normalize(kind, result, {}) == normalize(kind, payload, {}):
                    return result
            except Exception:
                pass
            raise AmbiguousMutation("Mutation outcome requires reconciliation") from exc

    def _save(self, manifest: Manifest, operation: dict[str, Any], result: dict[str, Any]) -> None:
        kind, key = operation["type"], operation["local_key"]
        if not result.get("id") or not result.get("name"):
            raise AmbiguousMutation("Mutation did not return a stable remote identity")
        previous = self.store.resources(self.scope).get(operation["id"], {})
        self.store.save(self.scope, operation["id"], {
            **previous, "type": kind, "local_key": key,
            "remote_id": result["id"], "remote_name": result["name"],
            "desired_hash": manifest.resource_hash(kind, key),
            "last_applied": normalize(kind, result, self._reverse()),
            "remote_marker": result.get("updated_at"), "status": "applied", "timestamp": _now(),
        })


class AmbiguousMutation(DefinitionError):
    """One mutation was attempted; no automatic mutation retry is safe."""
