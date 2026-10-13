"""Wiki upload/status/run example. Inject a client; nothing runs on import."""

from pathlib import Path

from envai import AsyncEnvaiClient, EnvaiClient


def upload_and_stage(client: EnvaiClient, workspace_id: str, original: Path, key: str):
    accepted = client.wiki.upload_file(workspace_id, original, idempotency_key=key)
    # Conversion is asynchronous. Schedule subsequent status reads in your app.
    return client.wiki.source(workspace_id, accepted.file_id)


async def upload_and_stage_async(
    client: AsyncEnvaiClient, workspace_id: str, original: Path, key: str
):
    accepted = await client.wiki.upload_file(workspace_id, original, idempotency_key=key)
    return await client.wiki.source(workspace_id, accepted.file_id)


def start_inbox(client: EnvaiClient, workspace_id: str, webhook_url: str, key: str):
    return client.wiki.inbox(workspace_id, body={"webhook_url": webhook_url}, idempotency_key=key)
