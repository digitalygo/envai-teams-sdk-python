"""Explicit synchronous stream; uses your configured existing chat."""

import os

from envai import EnvaiClient
from envai.event_models import AgentTextDeltaEvent

with EnvaiClient(os.environ["ENVAI_API_KEY"], base_url=os.environ["ENVAI_BASE_URL"]) as client:
    for event in client.chats.send_and_stream(os.environ["ENVAI_CHAT_ID"], "Hello"):
        if (
            isinstance(event, AgentTextDeltaEvent)
            and event.agent
            and event.agent.role == "orchestrator"
        ):
            print(event.data.delta, end="", flush=True)
        if event.terminal:
            print("\n", event.type, event.seq)
