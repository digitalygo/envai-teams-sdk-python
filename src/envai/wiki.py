"""Upload-first Wiki V1 services; GET catalog reads are exhaustive and deterministic.

QUERY/artifact writes are internal actor capabilities, not public HTTP endpoints.
"""

from __future__ import annotations

import mimetypes
import threading
import time
from pathlib import Path
from typing import Any, Literal

from . import _generated as generated
from ._transport import RequestOptions, check_cancel, poll_sleep
from .errors import ProtocolError
from .models import WikiOperation, WikiSource

MAX_SOURCE_BYTES = 1_000_000_000


def _validate_processing(processing: str) -> None:
    if not isinstance(processing, str) or processing not in ("convert", "original"):
        raise ValueError("Wiki source processing must be convert or original")


def _validate_upload(files: Any) -> None:
    values = dict(files)
    if set(values) != {"file"}:
        raise ValueError("Wiki upload requires exactly one file")
    value = values["file"]
    content = value[1] if isinstance(value, tuple) else value
    if isinstance(content, (bytes, str)):
        size = len(content.encode() if isinstance(content, str) else content)
    else:
        try:
            position = content.tell()
            content.seek(0, 2)
            size = content.tell()
            content.seek(position)
        except (AttributeError, OSError) as exc:
            raise ValueError("Upload needs bytes or a seekable binary file") from exc
    if size > MAX_SOURCE_BYTES:
        raise ValueError("Wiki original exceeds 1,000,000,000 bytes")


def _upload(path: str | Path):
    file = Path(path).open("rb")
    import os

    if os.fstat(file.fileno()).st_size > MAX_SOURCE_BYTES:
        file.close()
        raise ValueError("Wiki original exceeds 1,000,000,000 bytes")
    return file


def _index_options(options: dict[str, Any]) -> None:
    start, stop, all_ = options.get("from_"), options.get("to"), options.get("all")
    if all_ is True:
        if start is not None or stop is not None:
            raise ValueError("Choose all=True or an inclusive range")
    elif type(start) is not int or type(stop) is not int or not 1 <= start <= stop:
        raise ValueError("Provide all=True or 1 <= from_ <= to")


def _source_done(source: WikiSource) -> bool:
    return (source.lifecycle in ("deleted", "deleting", "outbox") or
            source.conversion.status == "completed" or
            (source.conversion.status == "not_required" and source.lifecycle in ("inbox", "raw")) or
            (source.conversion.status == "failed" and not source.conversion.recoverable))


class WikiService(generated.WikiService):
    def wait_source(
        self, workspace_id: str, file_id: str, *, timeout: float = 120,
        poll_interval: float = 1, cancel: threading.Event | None = None,
    ) -> WikiSource:
        """Poll identity-checked state only; never trigger processing or indexing."""
        from .client import _poll_bounds
        _poll_bounds(timeout, poll_interval)
        deadline = time.monotonic()+timeout
        options = RequestOptions(deadline=deadline, cancel=cancel)
        while True:
            check_cancel(options)
            value = self.source(workspace_id, file_id, request_options=options)
            if value.workspace_id != workspace_id or value.file_id != file_id:
                raise ProtocolError("Wiki polling returned a different identity")
            if _source_done(value):
                return value
            if cancel is not None:
                cancel.wait(min(poll_interval, max(0, deadline-time.monotonic())))
            else:
                time.sleep(min(poll_interval, max(0, deadline-time.monotonic())))

    def wait_operation(
        self, workspace_id: str, operation_id: str, *, timeout: float = 120,
        poll_interval: float = 1, cancel: threading.Event | None = None,
    ) -> WikiOperation:
        """Poll identity-checked state only; never trigger processing or indexing."""
        from .client import _poll_bounds
        _poll_bounds(timeout, poll_interval)
        deadline = time.monotonic()+timeout
        options = RequestOptions(deadline=deadline, cancel=cancel)
        while True:
            check_cancel(options)
            value = self.operation(workspace_id, operation_id, request_options=options)
            if value.workspace_id != workspace_id or value.operation_id != operation_id:
                raise ProtocolError("Wiki polling returned a different identity")
            if value.status in ("completed", "failed"):
                return value
            if cancel is not None:
                cancel.wait(min(poll_interval, max(0, deadline-time.monotonic())))
            else:
                time.sleep(min(poll_interval, max(0, deadline-time.monotonic())))

    def upload_file(
        self,
        workspace_id: str,
        path: str | Path,
        *,
        idempotency_key: str,
        processing: Literal["convert", "original"] = "convert",
        request_options: RequestOptions | None = None,
    ) -> WikiSource:
        _validate_processing(processing)
        with _upload(path) as file:
            return self.upload(
                workspace_id,
                files={
                    "file": (
                        Path(path).name,
                        file,
                        mimetypes.guess_type(str(path))[0] or "application/octet-stream",
                    )
                },
                data={"processing": "original"} if processing == "original" else None,
                idempotency_key=idempotency_key,
                request_options=request_options,
            )

    def category_index(self, workspace_id: str, category_id: str, **options: Any):
        _index_options(options)
        return super().category_index(workspace_id, category_id, **options)


class AsyncWikiService(generated.AsyncWikiService):
    async def wait_source(
        self, workspace_id: str, file_id: str, *, timeout: float = 120,
        poll_interval: float = 1, cancel: threading.Event | None = None,
    ) -> WikiSource:
        """Poll identity-checked state only; never trigger processing or indexing."""
        from .client import _poll_bounds
        _poll_bounds(timeout, poll_interval)
        deadline = time.monotonic()+timeout
        options = RequestOptions(deadline=deadline, cancel=cancel)
        while True:
            check_cancel(options)
            value = await self.source(workspace_id, file_id, request_options=options)
            if value.workspace_id != workspace_id or value.file_id != file_id:
                raise ProtocolError("Wiki polling returned a different identity")
            if _source_done(value):
                return value
            await poll_sleep(min(poll_interval, max(0, deadline-time.monotonic())), options)

    async def wait_operation(
        self, workspace_id: str, operation_id: str, *, timeout: float = 120,
        poll_interval: float = 1, cancel: threading.Event | None = None,
    ) -> WikiOperation:
        """Poll identity-checked state only; never trigger processing or indexing."""
        from .client import _poll_bounds
        _poll_bounds(timeout, poll_interval)
        deadline = time.monotonic()+timeout
        options = RequestOptions(deadline=deadline, cancel=cancel)
        while True:
            check_cancel(options)
            value = await self.operation(workspace_id, operation_id, request_options=options)
            if value.workspace_id != workspace_id or value.operation_id != operation_id:
                raise ProtocolError("Wiki polling returned a different identity")
            if value.status in ("completed", "failed"):
                return value
            await poll_sleep(min(poll_interval, max(0, deadline-time.monotonic())), options)

    async def upload_file(
        self,
        workspace_id: str,
        path: str | Path,
        *,
        idempotency_key: str,
        processing: Literal["convert", "original"] = "convert",
        request_options: RequestOptions | None = None,
    ) -> WikiSource:
        _validate_processing(processing)
        with _upload(path) as file:
            return await self.upload(
                workspace_id,
                files={
                    "file": (
                        Path(path).name,
                        file,
                        mimetypes.guess_type(str(path))[0] or "application/octet-stream",
                    )
                },
                data={"processing": "original"} if processing == "original" else None,
                idempotency_key=idempotency_key,
                request_options=request_options,
            )

    async def category_index(self, workspace_id: str, category_id: str, **options: Any):
        _index_options(options)
        return await super().category_index(workspace_id, category_id, **options)
