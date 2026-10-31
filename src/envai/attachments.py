from __future__ import annotations

import asyncio
import math
import mimetypes
import time
from pathlib import Path
from typing import Any

from . import _generated as generated
from ._transport import RequestOptions, check_cancel, poll_sleep
from .models import ChatAttachment
from .wiki import _upload


def _bounds(timeout: float, interval: float) -> None:
    if not all(math.isfinite(v) and v > 0 for v in (timeout, interval)):
        raise ValueError("Polling bounds must be finite and positive")


def _done(value: ChatAttachment) -> bool:
    return (
        value.lifecycle in ("deleting", "deleted")
        or value.conversion.status == "completed"
        or value.conversion.status == "not_required"
        and value.lifecycle == "ready"
        or value.conversion.status == "failed"
        and not value.conversion.recoverable
    )


def _file_part(path: str | Path, file: Any) -> dict[str, Any]:
    return {
        "file": (
            Path(path).name,
            file,
            mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        )
    }


class AttachmentsService(generated.AttachmentsService):
    def upload_file(
        self,
        workspace_id: str,
        chat_id: str,
        path: str | Path,
        *,
        idempotency_key: str,
        request_options: RequestOptions | None = None,
    ) -> ChatAttachment:
        check_cancel(request_options)
        with _upload(path) as file:
            return self.upload(
                workspace_id,
                chat_id,
                files=_file_part(path, file),
                idempotency_key=idempotency_key,
                request_options=request_options,
            )

    def wait(
        self,
        workspace_id: str,
        chat_id: str,
        attachment_id: str,
        *,
        timeout: float = 600,
        interval: float = 1,
        request_options: RequestOptions | None = None,
    ) -> ChatAttachment:
        _bounds(timeout, interval)
        deadline = time.monotonic() + timeout
        if request_options and request_options.deadline is not None:
            deadline = min(deadline, request_options.deadline)
        while True:
            check_cancel(request_options)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Attachment polling timed out")
            bounded = RequestOptions(
                timeout=min(remaining, request_options.timeout or remaining)
                if request_options
                else remaining,
                cancel=request_options.cancel if request_options else None,
                deadline=deadline,
            )
            value = self.get(workspace_id, chat_id, attachment_id, request_options=bounded)
            if _done(value):
                return value
            wait = min(interval, max(0, deadline - time.monotonic()))
            if request_options and request_options.cancel:
                request_options.cancel.wait(wait)
            else:
                time.sleep(wait)


class AsyncAttachmentsService(generated.AsyncAttachmentsService):
    async def upload_file(
        self,
        workspace_id: str,
        chat_id: str,
        path: str | Path,
        *,
        idempotency_key: str,
        request_options: RequestOptions | None = None,
    ) -> ChatAttachment:
        check_cancel(request_options)
        with _upload(path) as file:
            return await self.upload(
                workspace_id,
                chat_id,
                files=_file_part(path, file),
                idempotency_key=idempotency_key,
                request_options=request_options,
            )

    async def wait(
        self,
        workspace_id: str,
        chat_id: str,
        attachment_id: str,
        *,
        timeout: float = 600,
        interval: float = 1,
        request_options: RequestOptions | None = None,
    ) -> ChatAttachment:
        _bounds(timeout, interval)
        deadline = time.monotonic()+timeout
        if request_options and request_options.deadline is not None:
            deadline = min(deadline, request_options.deadline)
        options = RequestOptions(timeout=request_options.timeout if request_options else None,
            cancel=request_options.cancel if request_options else None, deadline=deadline)
        async with asyncio.timeout(timeout):
            while True:
                check_cancel(options)
                value = await self.get(
                    workspace_id, chat_id, attachment_id, request_options=options
                )
                if _done(value):
                    return value
                await poll_sleep(interval, options)
