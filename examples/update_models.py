"""Typed partial updates; omission and explicit null have different wire meanings."""
from envai import EnvaiClient, models


def update_definitions(client: EnvaiClient, workspace_id: str, tool_id: str,
                       agent_id: str, team_id: str) -> None:
    workspace = models.WorkspaceUpdateRequest(name="Renamed workspace")
    tool = models.UserToolUpdateRequest(description=None)
    agent = models.AgentUpdateRequest(model=None, tools_enabled=[])
    team = models.TeamUpdateRequest(description=None, members=[])
    client.workspaces.update(workspace_id, body=workspace)
    client.tools.update(tool_id, body=tool)
    client.agents.update(agent_id, body=agent)
    client.teams.update(team_id, body=team)
