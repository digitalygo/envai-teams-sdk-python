"""Library-owned target stream through final origin-task output; no import-time I/O."""
from envai import AsyncEnvaiClient, ClientToolRegistry, SQLiteToolState, models


async def run_job(
    client: AsyncEnvaiClient, registry: ClientToolRegistry, store: SQLiteToolState,
    *, scope: str, origin_chat: str, request: models.TeamDelegationRequest,
    timeout: float = 300,
) -> models.DetachedTaskSnapshot:
    """Failed/cancelled are explicit states; transport/tool submission failures raise."""
    return await client.chats.delegate_and_wait(origin_chat, body=request,
        registry=registry, store=store, scope=scope, timeout=timeout)
