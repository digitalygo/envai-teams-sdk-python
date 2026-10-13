"""Create a workspace/chat and run one potentially billable turn.

Requires ENVAI_BASE_URL, ENVAI_API_KEY, and an existing ENVAI_TEAM_ID.
"""

import os

from envai import EnvaiClient, models


def main():
    with EnvaiClient(
        os.environ["ENVAI_API_KEY"], base_url=os.environ["ENVAI_BASE_URL"]
    ) as client:
        workspace = client.workspaces.create(
            body=models.WorkspaceCreateRequest(name="SDK quickstart")
        )
        print("workspace:", workspace.id)
        chat = client.chats.create(
            workspace.id, body=models.ChatCreateRequest(team_id=os.environ["ENVAI_TEAM_ID"])
        )
        print("chat:", chat.id)
        result = client.chats.send_and_wait(chat.id, "Say hello in one sentence.", timeout=120)
        print(result.text)
        print("terminal:", result.terminal_event.type)
        if result.terminal_event.type != "turn.completed":
            raise RuntimeError(f"Turn ended with {result.terminal_event.type}")


if __name__ == "__main__":
    main()
