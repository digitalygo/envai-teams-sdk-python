"""Opt-in delegation orchestration. Execution and origin chats remain distinct."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from . import models
from ._transport import RequestOptions, check_cancel, poll_sleep
from .errors import ProtocolError, TransportError
from .tools import AsyncBackgroundClientToolRunner, BackgroundClientToolRunner

_TERMINAL = frozenset(("completed", "failed", "cancelled"))


def _identity(task: models.DetachedTaskSnapshot, receipt: models.TeamDelegationReceipt,
              request: models.TeamDelegationRequest | None) -> None:
    if task.task_id != receipt.task_id or (task.target_chat_id is not None and
            task.target_chat_id != receipt.target_chat_id) or (request is not None and
            task.origin_turn_id != request.origin_turn_id):
        raise ProtocolError("Delegation snapshot identity differs from the requested job")


def _validate(service: Any, timeout: float, poll_interval: float) -> None:
    from .client import _poll_bounds
    from .errors import CompatibilityError
    _poll_bounds(timeout, poll_interval)
    if not service._client.native_tasks:
        raise CompatibilityError("Delegation orchestration requires native_tasks=True")


def run_job(service: Any, origin_chat: str, *, request: models.TeamDelegationRequest | None,
            receipt: models.TeamDelegationReceipt | None, registry: Any, store: Any, scope: str,
            timeout: float, poll_interval: float, cancel: threading.Event | None,
            verifier: Any = None) -> models.DetachedTaskSnapshot:
    """Own a bounded target stream, never retry a submission or implicitly cancel a job."""
    from .client import _require_job_lifecycle
    _validate(service, timeout, poll_interval)
    deadline = time.monotonic() + timeout
    options = RequestOptions(deadline=deadline, cancel=cancel)
    _require_job_lifecycle(service._client.capabilities.get(request_options=options))
    target = request.target_chat_id if request is not None else receipt.target_chat_id  # type: ignore[union-attr]
    ready, release, stop = threading.Event(), threading.Event(), threading.Event()
    errors: list[Exception] = []
    runner = BackgroundClientToolRunner(service._client, registry, store=store, scope=scope,
                                       chat_id=target, verifier=verifier)
    stream = service.events(target, max_duration=max(0.000001, deadline-time.monotonic()), cancel=stop)

    def check() -> None:
        check_cancel(options)
        if errors:
            raise errors[0]
        if runner.errors:
            raise runner.errors[0]

    def pump() -> None:
        try:
            with stream:
                for event in stream:
                    if event.type == "ping" and not ready.is_set():
                        ready.set()
                        while not release.wait(0.01):
                            if stop.is_set():
                                return
                    runner.consume(event)
                    if stop.is_set():
                        return
            if not stop.is_set():
                raise TransportError("Target stream ended before final job outcome")
        except Exception as exc:
            if not stop.is_set():
                errors.append(exc)

    worker = threading.Thread(target=pump, name="envai-delegation-stream", daemon=True)
    worker.start()
    try:
        while not ready.wait(0.01):
            check()
        check()
        if request is not None:
            receipt = service.delegate(origin_chat, body=request, request_options=options)
            if receipt.target_chat_id != target:
                raise ProtocolError("Delegation receipt has a different execution chat")
        assert receipt is not None
        while True:
            check()
            task = service.get_task(origin_chat, receipt.task_id, request_options=options)
            _identity(task, receipt, request)
            if task.status in _TERMINAL:
                runner.close(wait=False)
                while runner.pending:
                    check()
                    stop.wait(0.01)
                check()
                return task
            release.set()
            until = min(deadline, time.monotonic()+poll_interval)
            while time.monotonic() < until:
                check()
                stop.wait(min(0.01, max(0, until-time.monotonic())))
    finally:
        stop.set()
        release.set()
        runner.close(wait=False)
        stream.close()
        worker.join(timeout=0.1)


async def run_job_async(service: Any, origin_chat: str, *, request: models.TeamDelegationRequest | None,
                        receipt: models.TeamDelegationReceipt | None, registry: Any, store: Any,
                        scope: str, timeout: float, poll_interval: float,
                        cancel: threading.Event | None, verifier: Any = None) -> models.DetachedTaskSnapshot:
    from .client import _require_job_lifecycle
    _validate(service, timeout, poll_interval)
    options = RequestOptions(deadline=time.monotonic()+timeout, cancel=cancel)
    target = request.target_chat_id if request is not None else receipt.target_chat_id  # type: ignore[union-attr]
    ready, release = asyncio.Event(), asyncio.Event()
    async with asyncio.timeout(timeout):
        _require_job_lifecycle(await service._client.capabilities.get(request_options=options))
        async with AsyncBackgroundClientToolRunner(service._client, registry, store=store,
                scope=scope, chat_id=target, verifier=verifier) as runner:
            async def pump() -> None:
                async with service.events(target, max_duration=timeout, cancel=cancel) as stream:
                    async for event in stream:
                        if event.type == "ping" and not ready.is_set():
                            ready.set()
                            await release.wait()
                        await runner.consume(event)
                raise TransportError("Target stream ended before final job outcome")

            async def execute() -> models.DetachedTaskSnapshot:
                nonlocal receipt
                await ready.wait()
                if request is not None:
                    receipt = await service.delegate(origin_chat, body=request, request_options=options)
                    if receipt.target_chat_id != target:
                        raise ProtocolError("Delegation receipt has a different execution chat")
                assert receipt is not None
                while True:
                    task = await service.get_task(origin_chat, receipt.task_id, request_options=options)
                    _identity(task, receipt, request)
                    if task.status in _TERMINAL:
                        await runner.aclose()
                        await runner.wait()
                        return task
                    release.set()
                    await poll_sleep(poll_interval, options)

            async def monitor() -> None:
                while True:
                    check_cancel(options)
                    if runner.errors:
                        raise runner.errors[0]
                    await asyncio.sleep(0.01)

            pump_task = asyncio.create_task(pump())
            job_task = asyncio.create_task(execute())
            monitor_task = asyncio.create_task(monitor())
            tasks = (pump_task, job_task, monitor_task)
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in (pump_task, monitor_task):
                    if task in done:
                        await task
                return await job_task
            finally:
                for pending_task in tasks:
                    pending_task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
