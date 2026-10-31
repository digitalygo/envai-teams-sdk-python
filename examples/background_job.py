"""Caller-owned target tool stream, kept alive through final origin-task output.

No provider call occurs on import. See README for identity and recovery limits.
"""

import asyncio
from contextlib import suppress

from envai import (
    AsyncBackgroundClientToolRunner,
    AsyncEnvaiClient,
    ClientToolRegistry,
    SQLiteToolState,
    models,
)


async def run_job(
    client: AsyncEnvaiClient,
    registry: ClientToolRegistry,
    store: SQLiteToolState,
    *,
    scope: str,
    origin_chat: str,
    request: models.TeamDelegationRequest,
    timeout: float = 300,
) -> models.DetachedTaskSnapshot:
    """Return failed/cancelled as explicit states; never auto-classify or retry a job."""
    ready = asyncio.Event()
    async with AsyncBackgroundClientToolRunner(
        client, registry, store=store, scope=scope, chat_id=request.target_chat_id
    ) as runner:
        async def pump():
            async with client.chats.events(request.target_chat_id, max_duration=timeout) as stream:
                async for event in stream:
                    await runner.consume(event)
                    if event.type == "ping":
                        ready.set()
            raise RuntimeError("Target tool stream ended before job completion")

        async def execute():
            await ready.wait()
            receipt = await client.chats.delegate(origin_chat, body=request)
            return await client.chats.wait_for_task(origin_chat, receipt.task_id, timeout=timeout)

        pump_task = asyncio.create_task(pump())
        job_task = asyncio.create_task(execute())
        try:
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait((pump_task, job_task),
                                             return_when=asyncio.FIRST_COMPLETED)
                if pump_task in done:
                    await pump_task
                return await job_task
        finally:
            for task in (job_task, pump_task):
                task.cancel()
            for task in (job_task, pump_task):
                with suppress(asyncio.CancelledError):
                    await task
