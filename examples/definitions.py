"""Offline validation; call deploy explicitly from your deployment command."""
from __future__ import annotations

from envai import EnvaiClient
from envai.definitions import ClientRemote, Definitions, FileStore, Manifest

DESIRED = Manifest({
    "format_version": "1",
    "tools": {"echo": {
        "name": "echo", "kind": "client", "description": "Echo text",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    }},
    "agents": {"assistant": {
        "name": "Example assistant", "system_prompt": "Help the user.", "mode": "primary",
        "external_tools_enabled": [{"name": "echo"}],
    }},
    "teams": {"example": {"name": "Example", "orchestrator_id": "assistant", "members": []}},
})


def deploy(client: EnvaiClient) -> dict:
    service = Definitions(ClientRemote(client), FileStore(".envai/definitions.json"),
                          scope="example-account/production")
    # Review plan before deployment. sync replans under one lock, never trusts a stale plan.
    print(service.plan(DESIRED))
    return service.sync(DESIRED)


if __name__ == "__main__":
    print(DESIRED.hash)  # No network, credentials, or state writes.
