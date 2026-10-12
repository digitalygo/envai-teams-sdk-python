"""Opt-in, durable deduplicated async echo tool for an existing configured chat.

The caller must register `echo` server-side and authorize it in the chosen agent.
No filesystem/shell tool is installed. The SQLite path is explicitly selected.
"""

import asyncio
import os

from envai import AsyncClientToolRunner, AsyncEnvaiClient, ClientToolRegistry, SQLiteToolState


async def main():
    registry = ClientToolRegistry()

    async def echo(args, context):
        context.check_cancelled()
        return {"echo": args["text"]}

    registry.register(
        "echo",
        echo,
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    with SQLiteToolState(os.environ["ENVAI_TOOL_STATE_PATH"]) as state:
        async with AsyncEnvaiClient(
            os.environ["ENVAI_API_KEY"], base_url=os.environ["ENVAI_BASE_URL"]
        ) as client:
            runner = None
            events = client.chats.send_and_stream(
                os.environ["ENVAI_CHAT_ID"], "Echo hello using the echo tool"
            )
            try:
                async for event in events:
                    if runner is None:
                        runner = AsyncClientToolRunner(
                            client,
                            registry,
                            store=state,
                            scope=os.environ["ENVAI_ACCOUNT_SCOPE"],
                            chat_id=event.chat_id,
                            turn_id=event.turn_id,
                        )
                    await runner.consume(event)
                    if event.terminal:
                        print(event.type, event.seq)
            finally:
                await events.aclose()
                if runner:
                    await runner.aclose()
                    await runner.wait()  # Surface delivery failures without repeating effects.


if __name__ == "__main__":
    asyncio.run(main())
