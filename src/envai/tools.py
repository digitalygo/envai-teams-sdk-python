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
from jsonschema.validators import validator_for
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, UnknownDialect, specification_with

from .errors import CompatibilityError, ProtocolError
from .events import Event, parse_event
from .tool_state import ToolStateStore
from .tool_verification import AsyncJwksVerifier, JwksVerifier, normalize_arguments

TASK_TERMINAL_EVENTS = frozenset(("task.completed", "task.failed", "task.cancelled"))


def _schema_validator(schema):
    # An explicit dialect must never silently fall back to another draft.
    if "$schema" in schema:
        try:
            specification_with(schema["$schema"])
        except (UnknownDialect, AttributeError) as exc:
            raise ValueError("Unsupported JSON Schema dialect") from exc
    return validator_for(schema, default=Draft202012Validator)


def _local_refs(resource: Resource) -> None:
    schema = resource.contents
    if isinstance(schema, dict):
        _schema_validator(schema)
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            if keyword in schema and (
                not isinstance(schema[keyword], str) or not schema[keyword].startswith("#")
            ):
                raise ValueError("External schema references are not permitted")
    # Traverse only schema locations for the declared draft. Values of const,
    # enum, default, and examples are literal data, even when they contain $ref.
    for child in resource.subresources():
        _local_refs(child)


@dataclass(frozen=True)
class ToolContext:
    chat_id: str
    turn_id: str
    call_id: str
    deadline: float
    cancelled: threading.Event = field(repr=False)
    task_id: str | None = None

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

        _schema_validator(parameters_schema).check_schema(parameters_schema)
        _local_refs(Resource.from_contents(parameters_schema, DRAFT202012))
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
    background = False

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
        if self.background and not client.native_tasks:
            raise CompatibilityError("Background runner requires native_tasks=True")
        self._call_tasks: dict[str, str] = {}
        self._ended_tasks: set[str] = set()
        self._ended_turns: set[str | None] = set()
        self.closed = False
        self.errors: list[Exception] = []

    def _check(self, event: Event) -> bool:
        # Revalidate even if an application constructed Event itself.
        parsed = parse_event(copy.deepcopy(event.raw))
        if parsed != event:
            raise ProtocolError("Event object does not match its signed wire data")
        if event.chat_id != self.chat_id:
            raise ProtocolError("Tool runner received a different chat")
        if event.type == "ping" or (not self.background and event.turn_id != self.turn_id):
            return False
        if self.background and event.type == "agent.tool.request":
            task_id = self._call_tasks.get(event.data.call_id)
            if task_id in self._ended_tasks or (
                task_id is None and event.turn_id in self._ended_turns
            ):
                return False
        return True

    def _snapshot(self, event: Event) -> Event:
        snapshot = parse_event(copy.deepcopy(event.raw))
        if snapshot != event:
            raise ProtocolError("Event object does not match its wire data")
        if self.background and snapshot.type == "task.event":
            wrapper = snapshot.raw["data"]
            if wrapper["event_type"] in ("agent.tool.request", "agent.tool.result"):
                inner = {**snapshot.raw, "type": wrapper["event_type"],
                         "turn_id": wrapper["origin_turn_id"], "data": wrapper["event_payload"]}
                snapshot = parse_event(inner)
                self._call_tasks[snapshot.data.call_id] = wrapper["task_id"]
        if snapshot.type == "agent.tool.request":
            snapshot.raw["data"]["args"] = normalize_arguments(snapshot.raw["data"]["args"])
            snapshot = parse_event(snapshot.raw)
        return snapshot

    def _cancel_event_contexts(self, event: Event) -> list[str]:
        with self._guard:
            if event.type in TASK_TERMINAL_EVENTS:
                self._ended_tasks.add(event.data.task_id)
            else:
                self._ended_turns.add(event.turn_id)
            matching = [context for context in self._contexts.values() if (
                context.task_id == event.data.task_id if event.type in TASK_TERMINAL_EVENTS
                else context.task_id is None and context.turn_id == event.turn_id
            )]
            for context in matching:
                context.cancelled.set()
                self._transition(context.call_id, "ambiguous")
            return [context.call_id for context in matching]

    def _claim(self, event: Event, payload: bytes) -> bool:
        return self.store.claim(
            self.scope,
            self.chat_id,
            event.data.call_id,
            hashlib.sha256(payload).hexdigest(),
            event.turn_id or self.turn_id,
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
            event.turn_id or self.turn_id,
            call.call_id,
            admitted + timeout * (1 - self.timeout_margin),
            threading.Event(),
            self._call_tasks.get(call.call_id),
        )
        if time.monotonic() >= context.deadline:
            return None, context, "tool_deadline_expired"
        if tool is None:
            return None, context, "tool_not_registered"
        if not _schema_validator(tool.schema)(tool.schema, registry=Registry()).is_valid(call.args):
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
            self.store.resolve(self.scope, self.chat_id, event.data.call_id, event.turn_id or self.turn_id)
            with self._guard:
                if context := self._contexts.get(event.data.call_id):
                    context.cancelled.set()
            return
        if self.background and (event.terminal or event.type in TASK_TERMINAL_EVENTS):
            self._cancel_event_contexts(event)
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
            self.store.resolve(self.scope, self.chat_id, event.data.call_id, event.turn_id or self.turn_id)
            if task := self._tasks.get(event.data.call_id):
                task.cancel()
            return
        if self.background and (event.terminal or event.type in TASK_TERMINAL_EVENTS):
            for call_id in self._cancel_event_contexts(event):
                if task := self._tasks.get(call_id):
                    task.cancel()
            return
        if event.terminal:
            await self.aclose()
            return
        if event.type != "agent.tool.request" or self.closed:
            return
        payload = await self.verifier.verify(event.raw["data"])
        if self.closed or not self._check(event):
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


class BackgroundReplay:
    """Buffer resident replay until ping, applying terminal barriers before requests."""

    def __init__(self):
        self.ready = False
        self.pending: list[Event] = []
        self.size = 0

    def accept(self, event: Event) -> list[Event]:
        snapshot = parse_event(copy.deepcopy(event.raw))
        if snapshot != event:
            raise ProtocolError("Event object does not match its wire data")
        if self.ready:
            return [snapshot]
        if snapshot.type != "ping":
            self.size += len(json.dumps(snapshot.raw, ensure_ascii=False).encode())
            if self.size > 16 * 1024 * 1024:
                raise ProtocolError("Background replay exceeds 16 MiB")
            self.pending.append(snapshot)
            return []
        self.ready = True
        # Results resolve durable call IDs before any replayed request can execute.
        barriers: list[Event] = []
        requests: list[Event] = []
        for item in self.pending:
            is_request = item.type == "agent.tool.request" or (
                item.type == "task.event" and item.data.event_type == "agent.tool.request"
            )
            (requests if is_request else barriers).append(item)
        self.pending = []
        self.size = 0
        return barriers + requests


class BackgroundClientToolRunner(ClientToolRunner):
    """Chat-wide runner surviving turn terminal and unwrapping signed task tools.

    Consume events from the execution chat starting with its replay through first
    ping. Keep the target subscription alive until the origin task is terminal.
    """

    background = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **{**kwargs, "turn_id": "background"})
        self._replay = BackgroundReplay()

    def consume(self, event: Event) -> None:
        if event.chat_id != self.chat_id:
            raise ProtocolError("Tool runner received a different chat")
        for current in self._replay.accept(event):
            super().consume(current)


class AsyncBackgroundClientToolRunner(AsyncClientToolRunner):
    """Async chat-wide task-aware counterpart of BackgroundClientToolRunner."""

    background = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **{**kwargs, "turn_id": "background"})
        self._replay = BackgroundReplay()

    async def consume(self, event: Event) -> None:
        if event.chat_id != self.chat_id:
            raise ProtocolError("Tool runner received a different chat")
        for current in self._replay.accept(event):
            await super().consume(current)
