"""Portable, side-effect-free definition compilation and canonical identities."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import rfc8785
import yaml


class DefinitionError(ValueError):
    """A local definition or reconciliation policy is invalid."""


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DefinitionError(f"Duplicate key: {key}")
        result[key] = value
    return result


class _Loader(yaml.SafeLoader):
    pass


def _mapping(loader: _Loader, node: Any) -> dict[str, Any]:
    return _pairs([(loader.construct_object(k), loader.construct_object(v, deep=True))
                   for k, v in node.value])


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text, object_pairs_hook=_pairs)
    return yaml.load(text, Loader=_Loader)


def _text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError(f"{field} must be a nonempty string")


def _forbid_tokens(value: Any) -> None:
    if isinstance(value, dict):
        if "max_tokens" in value:
            raise DefinitionError("config.max_tokens is unsupported")
        for item in value.values():
            _forbid_tokens(item)
    elif isinstance(value, list):
        for item in value:
            _forbid_tokens(item)


class Manifest:
    """Validated JSON-compatible desired state. No handler or credential serialization."""

    def __init__(self, document: Mapping[str, Any]):
        self.document = deepcopy(dict(document))
        if self.document.get("format_version") != "1":
            raise DefinitionError("format_version must be '1'")
        if set(self.document) - {"format_version", "tools", "agents", "teams"}:
            raise DefinitionError("Unknown manifest field")
        for collection in ("tools", "agents", "teams"):
            resources = self.document.setdefault(collection, {})
            if not isinstance(resources, dict):
                raise DefinitionError(f"{collection} must be an object")
            names: set[str] = set()
            for key, payload in resources.items():
                _text(key, "local key")
                if not isinstance(payload, dict):
                    raise DefinitionError("Resource must be an object")
                _text(payload.get("name"), "name")
                if payload["name"] in names:
                    raise DefinitionError("Duplicate remote name")
                names.add(payload["name"])
                if payload.get("description") is not None and not isinstance(payload["description"], str):
                    raise DefinitionError("Description must be a string or null")
                if collection == "tools":
                    if re.fullmatch(r"[a-z][a-z0-9_]{0,254}", key) is None:
                        raise DefinitionError("Invalid client tool name")
                    if key != payload["name"] or payload.get("kind", "client") != "client":
                        raise DefinitionError("Tool keys must equal client tool names")
                    payload.setdefault("kind", "client")
                    parameters = payload.get("parameters")
                    if not isinstance(parameters, dict) or parameters.get("type") != "object":
                        raise DefinitionError("Tool parameters must be an object schema")
                    timeout = payload.setdefault("timeout_seconds", 120)
                    if isinstance(timeout, bool) or not isinstance(timeout, int):
                        raise DefinitionError("Tool timeout must be an integer")
                    payload["timeout_seconds"] = max(1, min(600, timeout))
                elif collection == "agents":
                    _text(payload.get("system_prompt"), "system_prompt")
                    if payload.get("mode") not in ("primary", "subagent"):
                        raise DefinitionError("Agent mode must be primary or subagent")
                    for boolean in ("can_write_knowledge", "can_write_skills"):
                        if not isinstance(payload.setdefault(boolean, False), bool):
                            raise DefinitionError(f"{boolean} must be boolean")
                    if payload.get("model") is not None:
                        _text(payload["model"], "model")
                    for field in ("permissions", "config"):
                        if payload.get(field) is not None and not isinstance(payload[field], dict):
                            raise DefinitionError(f"{field} must be an object or null")
                    _forbid_tokens(payload.get("config"))
                    for field in ("tools_enabled", "external_tools_enabled"):
                        if field in payload and payload[field] is not None:
                            items = payload[field]
                            if not isinstance(items, list):
                                raise DefinitionError("Tool references must be lists")
                            for item in items:
                                if not isinstance(item, dict) or set(item) != {"name"}:
                                    raise DefinitionError("Tool reference must contain only name")
                                _text(item["name"], "tool name")
                            payload[field] = [{"name": name} for name in
                                              sorted({item["name"] for item in items})]
        for agent in self.document["agents"].values():
            for tool in agent.get("external_tools_enabled") or []:
                if tool["name"] not in self.document["tools"]:
                    raise DefinitionError("Unknown client tool reference")
        for team in self.document["teams"].values():
            orchestrator = self.document["agents"].get(team.get("orchestrator_id"))
            if not orchestrator or orchestrator["mode"] != "primary":
                raise DefinitionError("Team orchestrator must reference a primary agent")
            members = team.get("members")
            if not isinstance(members, list):
                raise DefinitionError("Team members must be a list")
            seen = {team["orchestrator_id"]}
            for member in members:
                if not isinstance(member, dict):
                    raise DefinitionError("Member must be an object")
                key = member.get("agent_definition_id")
                agent = self.document["agents"].get(key)
                if key in seen or not agent or agent["mode"] != "subagent":
                    raise DefinitionError("Team members must reference distinct subagents")
                seen.add(key)
                if member.get("role") is not None and not isinstance(member["role"], str):
                    raise DefinitionError("Member role must be string or null")
        # Also reject non-JSON values, NaN, unsupported integer ranges, and invalid Unicode.
        canonical_hash(self.document)

    @property
    def hash(self) -> str:
        return canonical_hash(self.document)

    def resource_hash(self, kind: str, key: str) -> str:
        document: dict[str, Any] = {"format_version": "1", "tools": {}, "agents": {}, "teams": {}}
        document[kind + "s"][key] = self.document[kind + "s"][key]
        return canonical_hash(document)


def markdown_agent(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise DefinitionError("Markdown requires YAML frontmatter")
    front, body = text[4:].split("\n---\n", 1)
    data = yaml.load(front, Loader=_Loader)
    if not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] != 1:
        raise DefinitionError("Markdown format must be integer 1")
    data.pop("format")
    tools = data.pop("tools", None)
    if tools is not None:
        if not isinstance(tools, dict) or set(tools) - {"server", "client"}:
            raise DefinitionError("Markdown tools require server/client lists")
        for source, target in (("server", "tools_enabled"), ("client", "external_tools_enabled")):
            if source in tools:
                if not isinstance(tools[source], list):
                    raise DefinitionError("Markdown tool references must be lists")
                data[target] = [{"name": name} for name in tools[source]]
    data.setdefault("name", path.stem)
    data["system_prompt"] = body.strip()
    return data


def compile_manifest(source: Manifest | dict[str, Any] | str | Path) -> Manifest:
    if isinstance(source, Manifest):
        return Manifest(source.document)
    if isinstance(source, Mapping):
        return Manifest(source)
    path = Path(source)
    if path.is_file():
        return Manifest(_load(path))
    if not path.is_dir():
        raise DefinitionError("Definition path does not exist")
    manifests = [p for p in path.iterdir() if p.name in
                 ("manifest.json", "manifest.yaml", "manifest.yml")]
    if manifests:
        if len(manifests) != 1:
            raise DefinitionError("Multiple manifests")
        return Manifest(_load(manifests[0]))
    document: dict[str, Any] = {"format_version": "1", "tools": {}, "agents": {}, "teams": {}}
    for kind in ("tools", "agents", "teams"):
        for file in sorted((path / kind).glob("*")):
            if file.suffix not in (".json", ".yaml", ".yml", ".md"):
                continue
            if file.stem in document[kind]:
                raise DefinitionError("Duplicate local key")
            if file.suffix == ".md" and kind != "agents":
                raise DefinitionError("Markdown is supported for agents only")
            document[kind][file.stem] = markdown_agent(file) if file.suffix == ".md" else _load(file)
    return Manifest(document)
