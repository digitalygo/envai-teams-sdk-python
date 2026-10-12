"""Caller-installed client tools. No tool is registered or executed implicitly."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import Draft202012Validator
from referencing import Registry

from .errors import ProtocolError
from .events import Event, parse_event
from .tool_state import ToolStateStore
from .tool_verification import AsyncJwksVerifier, JwksVerifier, normalize_arguments


@dataclass(frozen=True)
class ToolContext:
    chat_id: str
    turn_id: str
    call_id: str
    deadline: float
    cancelled: threading.Event = field(repr=False)

    def check_cancelled(self) -> None:
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError("Client tool cancelled or deadline exceeded")


@dataclass(frozen=True)
class LocalTool:
    handler: Callable[[dict[str, Any], ToolContext], Any] = field(repr=False)
    schema: dict[str, Any] = field(repr=False)
    timeout: float = 120


class ClientToolRegistry:
    def __init__(self):
        self._tools: dict[str, LocalTool] = {}

    def register(
        self,
        name: str,
        handler: Callable[[dict[str, Any], ToolContext], Any],
        parameters_schema: dict[str, Any],
        *,
        timeout: float = 120,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,254}", name) or not callable(handler):
            raise ValueError("Invalid tool name or handler")
        if not 0 < timeout <= 600 or parameters_schema.get("type") != "object":
            raise ValueError("Tools require an object schema and timeout in (0,600]")
        parameters_schema = copy.deepcopy(parameters_schema)

        def local_refs(value):
            if isinstance(value, dict):
                if any(
                    key in value and not value[key].startswith("#")
                    for key in ("$ref", "$dynamicRef", "$recursiveRef")
                ):
                    raise ValueError("External schema references are not permitted")
                for child in value.values():
                    local_refs(child)
            elif isinstance(value, list):
                for child in value:
                    local_refs(child)

        local_refs(parameters_schema)
        Draft202012Validator.check_schema(parameters_schema)
        if name in self._tools:
            raise ValueError("Tool already registered locally")
        self._tools[name] = LocalTool(handler, parameters_schema, timeout)

    def get(self, name: str) -> LocalTool | None:
        tool = self._tools.get(name)
        return LocalTool(tool.handler, copy.deepcopy(tool.schema), tool.timeout) if tool else None


def _output(value: Any, error: bool = False) -> dict[str, Any]:
    try:
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
        if text == "":
            return {"output": '{"error":"empty_tool_output"}', "is_error": True}
        if len(text.encode("utf-8")) > 128 * 1024:
            raise ValueError("Output too large")
        return {"output": text, "is_error": error}
    except (TypeError, ValueError, UnicodeError):
        return {"output": '{"error":"invalid_tool_output"}', "is_error": True}


class RunnerState:
    def __init__(
        self,
        client: Any,
        registry: ClientToolRegistry,
        *,
        store: ToolStateStore,
        scope: str,
        chat_id: str,
        turn_id: str,
        max_concurrency: int = 4,
        timeout_margin: float = 0.1,
    ):
        if (
            not scope
            or not chat_id
            or not turn_id
            or not isinstance(max_concurrency, int)
            or max_concurrency <= 0
            or not 0 < timeout_margin < 1
        ):
            raise ValueError("Explicit scope/chat/turn/store and valid bounds are required")
        if store is None:
            raise ValueError("A deduplication store is required; choose durable storage explicitly")
        self.client, self.registry, self.store = client, registry, store
        self.scope, self.chat_id, self.turn_id = scope, chat_id, turn_id
        self.max_concurrency, self.timeout_margin = max_concurrency, timeout_margin
        self._contexts: dict[str, ToolContext] = {}
        self._guard = threading.RLock()
        self.closed = False
        self.errors: list[Exception] = []

    def _check(self, event: Event) -> bool:
        # Revalidate even if an application constructed Event itself.
        parsed = parse_event(copy.deepcopy(event.raw))
        if parsed != event:
            raise ProtocolError("Event object does not match its signed wire data")
        if event.chat_id != self.chat_id:
            raise ProtocolError("Tool runner received a different chat")
        if event.type == "ping" or event.turn_id != self.turn_id:
            return False
        return True

    def _snapshot(self, event: Event) -> Event:
        snapshot = parse_event(copy.deepcopy(event.raw))
        if snapshot != event:
            raise ProtocolError("Event object does not match its wire data")
        if snapshot.type == "agent.tool.request":
            snapshot.raw["data"]["args"] = normalize_arguments(snapshot.raw["data"]["args"])
            snapshot = parse_event(snapshot.raw)
        return snapshot

    def _claim(self, event: Event, payload: bytes) -> bool:
        return self.store.claim(
            self.scope,
            self.chat_id,
            event.data.call_id,
            hashlib.sha256(payload).hexdigest(),
            self.turn_id,
        )

    def _transition(self, call_id: str, status: str) -> None:
        self.store.transition(self.scope, self.chat_id, call_id, status)

    def _error(self, exc: Exception):
        self.errors.append(exc)
        self.client._observe("tool.error", error=type(exc).__name__)

    def _prepare(
        self, event: Event, admitted: float
    ) -> tuple[LocalTool | None, ToolContext, str | None]:
        call = event.data
        tool = self.registry.get(call.tool_name)
        timeout = tool.timeout if tool else 1
        context = ToolContext(
            self.chat_id,
            self.turn_id,
            call.call_id,
            admitted + timeout * (1 - self.timeout_margin),
            threading.Event(),
        )
        if time.monotonic() >= context.deadline:
            return None, context, "tool_deadline_expired"
        if tool is None:
            return None, context, "tool_not_registered"
        if not Draft202012Validator(tool.schema, registry=Registry()).is_valid(call.args):
            return None, context, "invalid_tool_arguments"
        return tool, context, None


class ClientToolRunner(RunnerState):
    """Bounded sync worker threads with cooperative cancellation.

    Handlers MUST poll context.check_cancelled() before committing effects.
    Python cannot forcibly stop arbitrary threads; timed-out handlers retain their
    concurrency slot until they exit. No additional handler is started in its place.
    """

    def __init__(self, *args: Any, verifier: JwksVerifier | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.verifier = verifier or JwksVerifier(
            lambda: self.client.get_jwks().model_dump(exclude_unset=True)
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrency, thread_name_prefix="envai-tool"
        )
        self._futures: dict[str, Future] = {}

    def consume(self, event: Event) -> None:
        admitted = time.monotonic()
        event = self._snapshot(event)
        if not self._check(event):
            return
        if event.type == "agent.tool.result":
            self.store.resolve(self.scope, self.chat_id, event.data.call_id, self.turn_id)
            with self._guard:
                if context := self._contexts.get(event.data.call_id):
                    context.cancelled.set()
            return
        if event.terminal:
            self.close(wait=False)
            return
        if event.type != "agent.tool.request" or self.closed:
            return
        payload = self.verifier.verify(event.raw["data"])
        if not self._claim(event, payload):
            self.client._observe("tool.duplicate", call_id=event.data.call_id)
            return
        tool, context, error = self._prepare(event, admitted)
        with self._guard:
            if self.closed:
                self._transition(context.call_id, "ambiguous")
                return
            if len(self._contexts) >= self.max_concurrency:
                self._deliver(context, _output({"error": "tool_capacity_exceeded"}, True))
                return
            self._contexts[context.call_id] = context
            future = self._executor.submit(self._execute, event, tool, context, error)
            self._futures[context.call_id] = future

    def _execute(self, event, tool, context, error):
        timer = threading.Timer(max(0, context.deadline - time.monotonic()), context.cancelled.set)
        timer.daemon = True
        timer.start()
        try:
            if error:
                result = _output({"error": error}, True)
            else:
                try:
                    context.check_cancelled()
                    value = tool.handler(event.data.args, context)
                    if inspect.isawaitable(value):
                        if inspect.iscoroutine(value):
                            value.close()
                        raise TypeError("Async handler registered on sync runner")
                    result = _output(value)
                except Exception:
                    result = _output({"error": "tool_handler_failed"}, True)
            if context.cancelled.is_set():
                self._transition(context.call_id, "ambiguous")
                return
            self._deliver(context, result)
        except Exception as exc:
            self._error(exc)
        finally:
            timer.cancel()
            with self._guard:
                self._contexts.pop(context.call_id, None)

    def _deliver(self, context, result):
        old = self.store.get(self.scope, self.chat_id, context.call_id)
        if context.cancelled.is_set() or self.closed or (old and old.status == "resolved"):
            return
        self._transition(context.call_id, "ambiguous")  # Persist BEFORE the mutating request.
        self.client.chats.submit_tool_output(
            self.chat_id, body={"call_id": context.call_id, **result}
        )
        self._transition(context.call_id, "submitted")

    def wait(self) -> None:
        for future in list(self._futures.values()):
            future.result()
        if self.errors:
            raise self.errors[0]

    def close(self, *, wait: bool = True) -> None:
        with self._guard:
            self.closed = True
            for context in self._contexts.values():
                context.cancelled.set()
                self._transition(context.call_id, "ambiguous")
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AsyncClientToolRunner(RunnerState):
    """Native async handlers with bounded task count and cancellation.

    Only async handlers are accepted at execution. Blocking effects belong in the
    sync runner with cooperative cancellation; no hidden executor bypass exists.
    """

    def __init__(self, *args: Any, verifier: AsyncJwksVerifier | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)

        async def fetch():
            return (await self.client.get_jwks()).model_dump(exclude_unset=True)

        self.verifier = verifier or AsyncJwksVerifier(fetch)
        self._tasks: dict[str, asyncio.Task] = {}

    async def consume(self, event: Event) -> None:
        admitted = time.monotonic()
        event = self._snapshot(event)
        if not self._check(event):
            return
        if event.type == "agent.tool.result":
            self.store.resolve(self.scope, self.chat_id, event.data.call_id, self.turn_id)
            if task := self._tasks.get(event.data.call_id):
                task.cancel()
            return
        if event.terminal:
            await self.aclose()
            return
        if event.type != "agent.tool.request" or self.closed:
            return
        payload = await self.verifier.verify(event.raw["data"])
        if self.closed:
            return
        if not self._claim(event, payload):
            self.client._observe("tool.duplicate", call_id=event.data.call_id)
            return
        tool, context, error = self._prepare(event, admitted)
        if len(self._contexts) >= self.max_concurrency:
            await self._deliver(context, _output({"error": "tool_capacity_exceeded"}, True))
            return
        self._contexts[context.call_id] = context
        self._tasks[context.call_id] = asyncio.create_task(
            self._execute(event, tool, context, error)
        )

    async def _execute(self, event, tool, context, error):
        try:
            if error:
                result = _output({"error": error}, True)
            else:
                try:
                    if not inspect.iscoroutinefunction(tool.handler):
                        raise TypeError("Async runner requires an async handler")
                    async with asyncio.timeout(max(0, context.deadline - time.monotonic())):
                        result = _output(await tool.handler(event.data.args, context))
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    result = _output({"error": "tool_handler_timeout"}, True)
                except Exception:
                    result = _output({"error": "tool_handler_failed"}, True)
            await self._deliver(context, result)
        except asyncio.CancelledError:
            context.cancelled.set()
            self._transition(context.call_id, "ambiguous")
            raise
        except Exception as exc:
            self._error(exc)
        finally:
            self._contexts.pop(context.call_id, None)

    async def _deliver(self, context, result):
        old = self.store.get(self.scope, self.chat_id, context.call_id)
        if context.cancelled.is_set() or self.closed or (old and old.status == "resolved"):
            return
        self._transition(context.call_id, "ambiguous")
        await self.client.chats.submit_tool_output(
            self.chat_id, body={"call_id": context.call_id, **result}
        )
        self._transition(context.call_id, "submitted")

    async def wait(self) -> None:
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        if self.errors:
            raise self.errors[0]

    async def aclose(self) -> None:
        self.closed = True
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
