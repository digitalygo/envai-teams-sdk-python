"""Public sync/async clients and high-level turn helpers."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from typing import Any, AsyncGenerator, Callable, Generator
from urllib.parse import parse_qsl, urlsplit

import httpx

from . import _generated as generated
from . import models
from ._transport import (
    AsyncTransport,
    RequestOptions,
    RetryPolicy,
    SyncTransport,
    check_cancel,
    poll_sleep,
)
from .attachments import AsyncAttachmentsService, AttachmentsService
from .errors import CompatibilityError, ProtocolError, TransportError
from .events import Event, TurnAccumulator, TurnResult
from .stream import AsyncEventStream, EventStream
from .tool_state import ToolStateStore
from .tool_verification import AsyncJwksVerifier, JwksVerifier
from .tools import ClientToolRegistry
from .wiki import AsyncWikiService, WikiService

TASK_TERMINAL_STATES = frozenset(("completed", "failed", "cancelled"))


def _poll_bounds(timeout: float, poll_interval: float) -> None:
    if not all(math.isfinite(v) and v > 0 for v in (timeout, poll_interval)):
        raise ValueError("Polling bounds must be finite and positive")


def _require_job_lifecycle(capabilities: models.Capabilities) -> None:
    if "delegated_job_lifecycle" not in capabilities.features:
        raise CompatibilityError("Final job waiting requires delegated_job_lifecycle")


def _task_identity(task: models.DetachedTaskSnapshot, task_id: str) -> None:
    if task.task_id != task_id:
        raise ProtocolError("Task lookup returned a different task")


def _safe_replay(events: list[Event], turn_id: str) -> list[Event]:
    """Inspect the complete replay before exposing requests to any consumer."""
    selected = [event for event in events if event.turn_id == turn_id]
    ended = any(event.terminal for event in selected)
    resolved = {event.data.call_id for event in selected if event.type == "agent.tool.result"}
    return [event for event in selected if event.type != "agent.tool.request"
            or (not ended and event.data.call_id not in resolved)]


class ChatsService(generated.ChatsService):
    def delegate_and_wait(
        self, chat_id: str, *, body: models.TeamDelegationRequest,
        registry: ClientToolRegistry, store: ToolStateStore, scope: str,
        timeout: float = 300, poll_interval: float = 1,
        cancel: threading.Event | None = None, verifier: JwksVerifier | None = None,
    ) -> models.DetachedTaskSnapshot:
        """Keep target SSE alive and return final origin task output/result_turn_id.

        Reuse the exact request event_id to recover an uncertain admission. An
        existing running job resumes pending replay safely; no mutation is retried.
        """
        from .jobs import run_job
        return run_job(self, chat_id, request=body, receipt=None, registry=registry,
            store=store, scope=scope, timeout=timeout, poll_interval=poll_interval,
            cancel=cancel, verifier=verifier)

    def resume_delegation(
        self, chat_id: str, receipt: models.TeamDelegationReceipt, *,
        registry: ClientToolRegistry, store: ToolStateStore, scope: str,
        timeout: float = 300, poll_interval: float = 1,
        cancel: threading.Event | None = None, verifier: JwksVerifier | None = None,
    ) -> models.DetachedTaskSnapshot:
        """Resume a known receipt without resubmitting delegation or classification."""
        from .jobs import run_job
        return run_job(self, chat_id, request=None, receipt=receipt, registry=registry,
            store=store, scope=scope, timeout=timeout, poll_interval=poll_interval,
            cancel=cancel, verifier=verifier)

    def wait_for_task(
        self, chat_id: str, task_id: str, *, timeout: float = 120, poll_interval: float = 1,
        cancel: threading.Event | None = None
    ) -> models.DetachedTaskSnapshot:
        """Wait for the origin task's final output; keep target tool SSE open separately."""
        _poll_bounds(timeout, poll_interval)
        if not self._client.native_tasks:
            raise CompatibilityError("Final job waiting requires native_tasks=True")
        deadline = time.monotonic() + timeout
        options = RequestOptions(deadline=deadline, cancel=cancel)
        _require_job_lifecycle(self._client.capabilities.get(request_options=options))
        while time.monotonic() < deadline:
            task = self.get_task(chat_id, task_id, request_options=options)
            _task_identity(task, task_id)
            if task.status in TASK_TERMINAL_STATES:
                return task
            if cancel is not None:
                cancel.wait(min(poll_interval, max(0, deadline - time.monotonic())))
                check_cancel(options)
            else:
                time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
        raise TransportError("Task wait timed out; outcome remains unknown")

    def events(self, chat_id: str, **options: Any) -> EventStream:
        return EventStream(self._client, chat_id, **options)

    def send_and_stream(
        self,
        chat_id: str,
        message: str,
        *,
        timeout: float = 120,
        attachment_ids: list[str] | None = None,
        strict_model: bool = False,
        idempotency_key: str | None = None,
        **stream_options: Any,
    ) -> Generator[Event, None, None]:
        """Attach through first ping before sending once; yield only this turn."""
        _poll_bounds(timeout, 1)
        options = RequestOptions(deadline=time.monotonic()+timeout, cancel=stream_options.get("cancel"))
        with self.events(chat_id, max_duration=timeout, **stream_options) as stream:
            accepted = None
            replay: list[Event] = []
            replay_bytes = 0
            for event in stream:
                if accepted is None:
                    if event.type != "ping":
                        if idempotency_key:
                            replay_bytes += len(json.dumps(event.raw, ensure_ascii=False).encode())
                            if replay_bytes > 16 * 1024 * 1024:
                                raise ProtocolError("Idempotent send replay exceeds 16 MiB; resume using the known turn cursor")
                            replay.append(event)
                        continue
                    accepted = self.send_message(
                        chat_id,
                        body={"message": message, "attachment_ids": attachment_ids or [],
                              **({"strict_model": strict_model} if strict_model is not False else {})},
                        idempotency_key=idempotency_key, request_options=options,
                    )
                    for previous in _safe_replay(replay, accepted.turn_id):
                        if previous.turn_id == accepted.turn_id:
                            yield previous
                            if previous.terminal:
                                return
                    replay.clear()
                    continue
                if event.turn_id == accepted.turn_id:
                    yield event
                    if event.terminal:
                        return
        raise TransportError("Stream ended before matching terminal event")

    def send_and_wait(
        self,
        chat_id: str,
        message: str,
        *,
        timeout: float = 120,
        attachment_ids: list[str] | None = None,
        strict_model: bool = False,
        idempotency_key: str | None = None,
        **stream_options: Any,
    ) -> TurnResult:
        acc = TurnAccumulator()
        events = self.send_and_stream(
            chat_id,
            message,
            timeout=timeout,
            attachment_ids=attachment_ids,
            strict_model=strict_model,
            idempotency_key=idempotency_key,
            **stream_options,
        )
        try:
            for event in events:
                acc.accept(event)
                if event.terminal:
                    return TurnResult(chat_id, event.turn_id or "", acc.text, event, event.seq)
        finally:
            events.close()
        raise TransportError("Stream ended before matching terminal event")


class AsyncChatsService(generated.AsyncChatsService):
    async def delegate_and_wait(
        self, chat_id: str, *, body: models.TeamDelegationRequest,
        registry: ClientToolRegistry, store: ToolStateStore, scope: str,
        timeout: float = 300, poll_interval: float = 1,
        cancel: threading.Event | None = None, verifier: AsyncJwksVerifier | None = None,
    ) -> models.DetachedTaskSnapshot:
        """Keep target SSE alive and return final origin task output/result_turn_id.

        Reuse the exact request event_id to recover an uncertain admission. An
        existing running job resumes pending replay safely; no mutation is retried.
        """
        from .jobs import run_job_async
        return await run_job_async(self, chat_id, request=body, receipt=None, registry=registry,
            store=store, scope=scope, timeout=timeout, poll_interval=poll_interval,
            cancel=cancel, verifier=verifier)

    async def resume_delegation(
        self, chat_id: str, receipt: models.TeamDelegationReceipt, *,
        registry: ClientToolRegistry, store: ToolStateStore, scope: str,
        timeout: float = 300, poll_interval: float = 1,
        cancel: threading.Event | None = None, verifier: AsyncJwksVerifier | None = None,
    ) -> models.DetachedTaskSnapshot:
        """Resume a known receipt without resubmitting delegation or classification."""
        from .jobs import run_job_async
        return await run_job_async(self, chat_id, request=None, receipt=receipt, registry=registry,
            store=store, scope=scope, timeout=timeout, poll_interval=poll_interval,
            cancel=cancel, verifier=verifier)

    async def wait_for_task(
        self, chat_id: str, task_id: str, *, timeout: float = 120, poll_interval: float = 1,
        cancel: threading.Event | None = None
    ) -> models.DetachedTaskSnapshot:
        """Wait for the origin task's final output; keep target tool SSE open separately."""
        _poll_bounds(timeout, poll_interval)
        if not self._client.native_tasks:
            raise CompatibilityError("Final job waiting requires native_tasks=True")
        options = RequestOptions(deadline=time.monotonic() + timeout, cancel=cancel)
        async with asyncio.timeout(timeout):
            _require_job_lifecycle(await self._client.capabilities.get(request_options=options))
            while True:
                task = await self.get_task(chat_id, task_id, request_options=options)
                _task_identity(task, task_id)
                if task.status in TASK_TERMINAL_STATES:
                    return task
                await poll_sleep(poll_interval, options)

    def events(self, chat_id: str, **options: Any) -> AsyncEventStream:
        return AsyncEventStream(self._client, chat_id, **options)

    async def send_and_stream(
        self,
        chat_id: str,
        message: str,
        *,
        timeout: float = 120,
        attachment_ids: list[str] | None = None,
        strict_model: bool = False,
        idempotency_key: str | None = None,
        **stream_options: Any,
    ) -> AsyncGenerator[Event, None]:
        _poll_bounds(timeout, 1)
        options = RequestOptions(deadline=time.monotonic()+timeout, cancel=stream_options.get("cancel"))
        async with asyncio.timeout(timeout):
            async with self.events(chat_id, max_duration=timeout, **stream_options) as stream:
                accepted = None
                replay: list[Event] = []
                replay_bytes = 0
                async for event in stream:
                    if accepted is None:
                        if event.type != "ping":
                            if idempotency_key:
                                replay_bytes += len(json.dumps(event.raw, ensure_ascii=False).encode())
                                if replay_bytes > 16 * 1024 * 1024:
                                    raise ProtocolError("Idempotent send replay exceeds 16 MiB; resume using the known turn cursor")
                                replay.append(event)
                            continue
                        accepted = await self.send_message(
                            chat_id,
                            body={"message": message, "attachment_ids": attachment_ids or [],
                              **({"strict_model": strict_model} if strict_model is not False else {})},
                            idempotency_key=idempotency_key, request_options=options,
                        )
                        for previous in _safe_replay(replay, accepted.turn_id):
                            if previous.turn_id == accepted.turn_id:
                                yield previous
                                if previous.terminal:
                                    return
                        replay.clear()
                        continue
                    if event.turn_id == accepted.turn_id:
                        yield event
                        if event.terminal:
                            return
        raise TransportError("Stream ended before matching terminal event")

    async def send_and_wait(
        self,
        chat_id: str,
        message: str,
        *,
        timeout: float = 120,
        attachment_ids: list[str] | None = None,
        strict_model: bool = False,
        idempotency_key: str | None = None,
        **stream_options: Any,
    ) -> TurnResult:
        acc = TurnAccumulator()
        events = self.send_and_stream(
            chat_id,
            message,
            timeout=timeout,
            attachment_ids=attachment_ids,
            strict_model=strict_model,
            idempotency_key=idempotency_key,
            **stream_options,
        )
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
        self, *, body: Any, timeout: float = 120, poll_interval: float = 1,
        cancel: threading.Event | None = None
    ) -> Any:
        _poll_bounds(timeout, poll_interval)
        deadline = time.monotonic() + timeout
        options = RequestOptions(deadline=deadline, cancel=cancel)
        accepted = self.generate(body=body, request_options=options)
        while time.monotonic() < deadline:
            status = self.get(accepted.id, request_options=options)
            if status.status in ("succeeded", "failed", "canceled", "cancelled", "completed"):
                return status
            if cancel is not None:
                cancel.wait(min(poll_interval, max(0, deadline - time.monotonic())))
                check_cancel(options)
            else:
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
        self, *, body: Any, timeout: float = 120, poll_interval: float = 1,
        cancel: threading.Event | None = None
    ) -> Any:
        _poll_bounds(timeout, poll_interval)
        options = RequestOptions(deadline=time.monotonic()+timeout, cancel=cancel)
        async with asyncio.timeout(timeout):
            accepted = await self.generate(body=body, request_options=options)
            while True:
                status = await self.get(accepted.id, request_options=options)
                if status.status in ("succeeded", "failed", "canceled", "cancelled", "completed"):
                    return status
                await poll_sleep(poll_interval, options)


class CompletionsService(generated.CompletionsService):
    def iterate(self, workspace_id: str, chat_id: str, *,
                page: int = 1, request_options: RequestOptions | None = None, **filters: Any):
        """Use numeric pages through the common transport; never follow response URLs."""
        while True:
            result = self._client._request("listCompletions",
                path_params={"workspace_id": workspace_id, "chat_id": chat_id},
                query={**filters, "page": page}, headers={}, request_options=request_options)
            yield from result.data
            if not result.links.next:
                return
            if not result.meta.last_page or page >= result.meta.last_page:
                raise ProtocolError("Pagination metadata is inconsistent")
            page += 1


class AsyncCompletionsService(generated.AsyncCompletionsService):
    async def iterate(self, workspace_id: str, chat_id: str, *,
                      page: int = 1, request_options: RequestOptions | None = None, **filters: Any):
        while True:
            result = await self._client._request("listCompletions",
                path_params={"workspace_id": workspace_id, "chat_id": chat_id},
                query={**filters, "page": page}, headers={}, request_options=request_options)
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
        contract_version: int = 3,
        native_tasks: bool = False,
        timeout: float = 30,
        connect_timeout: float = 10,
        stream_read_timeout: float = 45,
        retry: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        observer: Callable[[str, dict[str, Any]], None] | None = None,
        headers: dict[str, str] | None = None,
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
            contract_version,
            native_tasks,
        )
        self._configure_headers(headers)
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
        self.wiki = WikiService(self)
        self.attachments = AttachmentsService(self)

    def stream_chat_events(self, chat_id: str, **options: Any) -> EventStream:
        return self.chats.events(chat_id, **options)

    def stream_chat_events_legacy(self, chat_id: str, **options: Any) -> EventStream:
        if self.contract_version != 2:
            raise CompatibilityError("Legacy replay requires explicit contract_version=2")
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
        contract_version: int = 3,
        native_tasks: bool = False,
        timeout: float = 30,
        connect_timeout: float = 10,
        stream_read_timeout: float = 45,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        observer: Callable[[str, dict[str, Any]], None] | None = None,
        headers: dict[str, str] | None = None,
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
            contract_version,
            native_tasks,
        )
        self._configure_headers(headers)
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
        self.wiki = AsyncWikiService(self)
        self.attachments = AsyncAttachmentsService(self)

    def stream_chat_events(self, chat_id: str, **options: Any) -> AsyncEventStream:
        return self.chats.events(chat_id, **options)

    def stream_chat_events_legacy(self, chat_id: str, **options: Any) -> AsyncEventStream:
        if self.contract_version != 2:
            raise CompatibilityError("Legacy replay requires explicit contract_version=2")
        return self.chats.events(chat_id, **options)

    async def aclose(self):
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
