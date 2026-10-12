"""Public sync/async clients and high-level turn helpers."""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncGenerator, Callable, Generator
from urllib.parse import parse_qsl, urlsplit

import httpx

from . import _generated as generated
from ._transport import AsyncTransport, RetryPolicy, SyncTransport
from .errors import ProtocolError, TransportError
from .events import Event, TurnAccumulator, TurnResult
from .stream import AsyncEventStream, EventStream


class ChatsService(generated.ChatsService):
    def events(self, chat_id: str, **options: Any) -> EventStream:
        return EventStream(self._client, chat_id, **options)

    def send_and_stream(
        self, chat_id: str, message: str, *, timeout: float = 120, **stream_options: Any
    ) -> Generator[Event, None, None]:
        """Attach through first ping before sending once; yield only this turn."""
        with self.events(chat_id, max_duration=timeout, **stream_options) as stream:
            accepted = None
            for event in stream:
                if accepted is None:
                    if event.type != "ping":
                        continue
                    accepted = self.send_message(chat_id, body={"message": message})
                    continue
                if event.turn_id == accepted.turn_id:
                    yield event
                    if event.terminal:
                        return
        raise TransportError("Stream ended before matching terminal event")

    def send_and_wait(
        self, chat_id: str, message: str, *, timeout: float = 120, **stream_options: Any
    ) -> TurnResult:
        acc = TurnAccumulator()
        events = self.send_and_stream(chat_id, message, timeout=timeout, **stream_options)
        try:
            for event in events:
                acc.accept(event)
                if event.terminal:
                    return TurnResult(chat_id, event.turn_id or "", acc.text, event, event.seq)
        finally:
            events.close()
        raise TransportError("Stream ended before matching terminal event")


class AsyncChatsService(generated.AsyncChatsService):
    def events(self, chat_id: str, **options: Any) -> AsyncEventStream:
        return AsyncEventStream(self._client, chat_id, **options)

    async def send_and_stream(
        self, chat_id: str, message: str, *, timeout: float = 120, **stream_options: Any
    ) -> AsyncGenerator[Event, None]:
        async with asyncio.timeout(timeout):
            async with self.events(chat_id, max_duration=timeout, **stream_options) as stream:
                accepted = None
                async for event in stream:
                    if accepted is None:
                        if event.type != "ping":
                            continue
                        accepted = await self.send_message(chat_id, body={"message": message})
                        continue
                    if event.turn_id == accepted.turn_id:
                        yield event
                        if event.terminal:
                            return
        raise TransportError("Stream ended before matching terminal event")

    async def send_and_wait(
        self, chat_id: str, message: str, *, timeout: float = 120, **stream_options: Any
    ) -> TurnResult:
        acc = TurnAccumulator()
        events = self.send_and_stream(chat_id, message, timeout=timeout, **stream_options)
        try:
            async for event in events:
                acc.accept(event)
                if event.terminal:
                    return TurnResult(chat_id, event.turn_id or "", acc.text, event, event.seq)
        finally:
            await events.aclose()
        raise TransportError("Stream ended before matching terminal event")


def _signed_url(client: Any, url: str) -> str:
    parts, base = urlsplit(url), urlsplit(client.base_url)
    if (
        parts.scheme != base.scheme
        or parts.netloc != base.netloc
        or parts.username
        or parts.password
    ):
        raise ValueError("Artifact URL must use the configured API origin")
    if parts.path != base.path + "/artifacts/download" or parts.fragment:
        raise ValueError("Artifact URL must use the current artifact endpoint")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    keys = [k for k, v in pairs]
    if (
        len(keys) != len(set(keys))
        or not {"artifact", "generation", "signature"}.issubset(keys)
        or set(keys) - {"artifact", "generation", "signature", "expires"}
    ):
        raise ValueError("Malformed artifact query")
    return url


class ImagesService(generated.ImagesService):
    def download_signed_url(self, url: str) -> bytes:
        response = self._client._http.get(
            _signed_url(self._client, url),
            headers={"Accept": "application/octet-stream"},
            timeout=self._client.timeout,
            follow_redirects=False,
        )
        return self._client._decode({"response": {}, "response_type": "bytes"}, response)

    def generate_and_wait(
        self, *, body: Any, timeout: float = 120, poll_interval: float = 1
    ) -> Any:
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("Polling bounds must be positive")
        accepted = self.generate(body=body)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.get(accepted.id)
            if status.status in ("succeeded", "failed", "canceled", "cancelled", "completed"):
                return status
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
        raise TransportError("Image generation timed out")


class AsyncImagesService(generated.AsyncImagesService):
    async def download_signed_url(self, url: str) -> bytes:
        response = await self._client._http.get(
            _signed_url(self._client, url),
            headers={"Accept": "application/octet-stream"},
            timeout=self._client.timeout,
            follow_redirects=False,
        )
        return self._client._decode({"response": {}, "response_type": "bytes"}, response)

    async def generate_and_wait(
        self, *, body: Any, timeout: float = 120, poll_interval: float = 1
    ) -> Any:
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("Polling bounds must be positive")
        async with asyncio.timeout(timeout):
            accepted = await self.generate(body=body)
            while True:
                status = await self.get(accepted.id)
                if status.status in ("succeeded", "failed", "canceled", "cancelled", "completed"):
                    return status
                await asyncio.sleep(poll_interval)


class CompletionsService(generated.CompletionsService):
    def iterate(self, workspace_id: str, chat_id: str, **filters: Any):
        """Use numeric pagination only; never follow untrusted cross-origin links."""
        page = 1
        while True:
            kwargs = dict(filters)
            op, url, request = self._client._prepare(
                "listCompletions",
                path_params={"workspace_id": workspace_id, "chat_id": chat_id},
                query=kwargs,
                headers={},
            )
            request["params"]["page"] = page
            response = self._client._http.get(url, **request)
            result = self._client._decode(op, response)
            yield from result.data
            if not result.links.next:
                return
            if not result.meta.last_page or page >= result.meta.last_page:
                raise ProtocolError("Pagination metadata is inconsistent")
            page += 1


class AsyncCompletionsService(generated.AsyncCompletionsService):
    async def iterate(self, workspace_id: str, chat_id: str, **filters: Any):
        page = 1
        while True:
            op, url, request = self._client._prepare(
                "listCompletions",
                path_params={"workspace_id": workspace_id, "chat_id": chat_id},
                query=filters,
                headers={},
            )
            request["params"]["page"] = page
            response = await self._client._http.get(url, **request)
            result = self._client._decode(op, response)
            for item in result.data:
                yield item
            if not result.links.next:
                return
            if not result.meta.last_page or page >= result.meta.last_page:
                raise ProtocolError("Pagination metadata is inconsistent")
            page += 1


class EnvaiClient(SyncTransport, generated.Operations):
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "http://localhost:8111/api/v1",
        jwks_url: str | None = None,
        timeout: float = 30,
        connect_timeout: float = 10,
        stream_read_timeout: float = 45,
        retry: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        observer: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self._configure(
            api_key,
            base_url,
            jwks_url,
            timeout,
            connect_timeout,
            stream_read_timeout,
            retry,
            observer,
        )
        self._http = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)
        self.workspaces = generated.WorkspacesService(self)
        self.tools = generated.ToolsService(self)
        self.chats = ChatsService(self)
        self.completions = CompletionsService(self)
        self.files = generated.FilesService(self)
        self.agents = generated.AgentsService(self)
        self.teams = generated.TeamsService(self)
        self.api_keys = generated.ApiKeysService(self)
        self.images = ImagesService(self)
        self.models = generated.ModelsService(self)
        self.capabilities = generated.CapabilitiesService(self)

    def stream_chat_events(self, chat_id: str, **options: Any) -> EventStream:
        return self.chats.events(chat_id, **options)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AsyncEnvaiClient(AsyncTransport, generated.AsyncOperations):
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "http://localhost:8111/api/v1",
        jwks_url: str | None = None,
        timeout: float = 30,
        connect_timeout: float = 10,
        stream_read_timeout: float = 45,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        observer: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self._configure(
            api_key,
            base_url,
            jwks_url,
            timeout,
            connect_timeout,
            stream_read_timeout,
            retry,
            observer,
        )
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)
        self.workspaces = generated.AsyncWorkspacesService(self)
        self.tools = generated.AsyncToolsService(self)
        self.chats = AsyncChatsService(self)
        self.completions = AsyncCompletionsService(self)
        self.files = generated.AsyncFilesService(self)
        self.agents = generated.AsyncAgentsService(self)
        self.teams = generated.AsyncTeamsService(self)
        self.api_keys = generated.AsyncApiKeysService(self)
        self.images = AsyncImagesService(self)
        self.models = generated.AsyncModelsService(self)
        self.capabilities = generated.AsyncCapabilitiesService(self)

    def stream_chat_events(self, chat_id: str, **options: Any) -> AsyncEventStream:
        return self.chats.events(chat_id, **options)

    async def aclose(self):
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
