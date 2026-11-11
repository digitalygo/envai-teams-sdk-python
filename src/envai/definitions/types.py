"""Typed native manifest builders; plain dictionaries remain accepted."""
from typing import Any, Literal, NotRequired, TypedDict


class ToolReference(TypedDict):
    name: str


class ToolDefinition(TypedDict):
    name: str
    parameters: dict[str, Any]
    kind: NotRequired[Literal["client"]]
    description: NotRequired[str | None]
    timeout_seconds: NotRequired[int]


class AgentDefinition(TypedDict):
    name: str
    system_prompt: str
    mode: Literal["primary", "subagent"]
    can_write_knowledge: NotRequired[bool]
    can_write_skills: NotRequired[bool]
    description: NotRequired[str | None]
    model: NotRequired[str | None]
    tools_enabled: NotRequired[list[ToolReference] | None]
    external_tools_enabled: NotRequired[list[ToolReference] | None]
    permissions: NotRequired[dict[str, Any] | None]
    config: NotRequired[dict[str, Any] | None]


class TeamMember(TypedDict):
    agent_definition_id: str
    role: NotRequired[str | None]


class TeamDefinition(TypedDict):
    name: str
    orchestrator_id: str
    members: list[TeamMember]
    description: NotRequired[str | None]


class ManifestDocument(TypedDict):
    format_version: Literal["1"]
    tools: NotRequired[dict[str, ToolDefinition]]
    agents: NotRequired[dict[str, AgentDefinition]]
    teams: NotRequired[dict[str, TeamDefinition]]
