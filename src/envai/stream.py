"""Single-reader, cancellable, best-effort resident-chat replay streams."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from typing import Any, AsyncIterator, Iterator
from urllib.parse import quote

import httpx

from ._schema import strict_json
from ._transport import delay
from .errors import (
    CancelledError,
    CompatibilityError,
    EnvaiError,
    ProtocolError,
    ReplayGapError,
    TransportError,
    from_response,
)
from .events import Event, SSEDecoder, parse_event


class StreamState:
    def __init__(
        self,
        client: Any,
        chat_id: str,
        *,
        after: int = 0,
        max_reconnects: int = 5,
        max_duration: float | None = None,
        cancel: threading.Event | None = None,
        max_frame_bytes: int = 2 * 1024 * 1024,
        max_snapshot_bytes: int = 64 * 1024 * 1024,
    ):
        if (
            not isinstance(after, int)
            or after < 0
            or not isinstance(max_reconnects, int)
            or max_reconnects < 0
            or (max_duration is not None and (not math.isfinite(max_duration) or max_duration <= 0))
        ):
            raise ValueError("Invalid stream bounds")
        if type(max_snapshot_bytes) is not int or max_snapshot_bytes <= 0:
            raise ValueError("max_snapshot_bytes must be positive")
        if not chat_id or chat_id in (".", ".."):
            raise ValueError("chat_id must be a nonempty opaque identifier")
        self.client, self.chat_id = client, chat_id
        self.contract_version = client.contract_version
        self.last_seq = after
        self.max_reconnects, self.max_duration = max_reconnects, max_duration
        self.cancel = cancel or threading.Event()
        self.max_frame_bytes = max_frame_bytes
        self.max_snapshot_bytes = max_snapshot_bytes
        self.closed = False
        self.live = False
        self.reconnects = 0
        self._started = time.monotonic()
        self._response: httpx.Response | None = None
        self._gap_pending = False
        self._retry_after: str | None = None
        self._lock = threading.Lock()

    def _check(self):
        if self.cancel.is_set():
            raise CancelledError("Stream cancelled locally")
        if self.max_duration is not None and time.monotonic() - self._started > self.max_duration:
            raise TransportError("Stream duration limit reached")

    def _event(self, raw: str, *, snapshot: bool = False) -> Event | None:
        event = parse_event(raw, contract_version=self.contract_version)
        if event.chat_id != self.chat_id:
            raise ProtocolError("SSE event belongs to a different chat", raw=event.raw)
        if event.type == "ping":
            self.live = True
            self.client._observe("stream.live", last_seq=self.last_seq)
            return event
        if event.seq <= self.last_seq:
            if snapshot:
                raise ProtocolError(
                    "Legacy replay contains a duplicate or backwards sequence", raw=event.raw
                )
            self.client._observe("stream.duplicate", seq=event.seq)
            return None
        if event.seq != self.last_seq + 1:
            raise ReplayGapError(self.last_seq + 1, event.seq)
        self._gap_pending = False
        return event

    def _snapshot(self, body: bytes) -> list[Event]:
        """Validate the entire finite legacy snapshot before exposing a prefix."""
        decoder = SSEDecoder(self.max_frame_bytes)
        frames = decoder.feed(body)
        if decoder._data or decoder._line:
            raise ProtocolError("Legacy replay snapshot ended in a partial frame")
        decoder.finish()
        before = self.last_seq
        result = []
        try:
            for raw in frames:
                event = self._event(raw, snapshot=True)
                if event is not None:
                    result.append(event)
                    if event.type != "ping":
                        self.last_seq = event.seq
        finally:
            self.last_seq = before
        return result

    def _request(self):
        headers = {
            **self.client._headers,
            "Accept": "text/event-stream",
            "X-Envai-Contract-Version": str(self.contract_version),
        }
        timeout = self.client.stream_timeout
        if self.max_duration is not None:
            self._check()
            remaining = max(0.001, self.max_duration - (time.monotonic() - self._started))
            timeout = httpx.Timeout(
                remaining,
                connect=min(remaining, timeout.connect),
                read=min(remaining, timeout.read),
            )
        return dict(
            method="GET",
            url=self.client.base_url
            + ("/atp/v3" if self.contract_version == 3 else "")
            + "/chats/"
            + quote(self.chat_id, safe="")
            + "/events",
            params={"after": self.last_seq},
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        )

    def _retry(self, exc: Exception | None) -> float:
        if isinstance(exc, ReplayGapError):
            if self._gap_pending:
                raise exc
            self._gap_pending = True
            self.client._observe("stream.gap", expected=exc.expected, received=exc.received)
        elif isinstance(exc, EnvaiError) and exc.http_status not in (429, 502, 503, 504):
            raise exc
        if self.reconnects >= self.max_reconnects:
            if isinstance(exc, httpx.TransportError):
                raise TransportError(
                    "SSE transport failed and reconnect limit was reached"
                ) from exc
            if exc:
                raise exc
            raise TransportError("SSE reconnect limit reached")
        self.reconnects += 1
        self.live = False
        self.client._observe("stream.reconnect", attempt=self.reconnects, after=self.last_seq)
        wait = (
            0
            if isinstance(exc, ReplayGapError)
            else delay(self.client.retry, self.reconnects - 1, self._retry_after)
        )
        if self.max_duration is not None:
            wait = min(wait, max(0, self.max_duration - (time.monotonic() - self._started)))
        return wait

    def _status(self, response: httpx.Response):
        self._retry_after = response.headers.get("retry-after")
        if response.status_code in (404, 426):
            raise CompatibilityError("Server cannot provide the selected ATP replay contract")
        if response.status_code != 200:
            try:
                payload = strict_json(response.content)
            except (ValueError, UnicodeError):
                payload = None
            raise from_response(response.status_code, payload, response.headers.get("x-request-id"))
        self.client._acknowledge(response, self.contract_version)
        if (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "text/event-stream"
        ):
            raise ProtocolError("Expected text/event-stream")


class EventStream(StreamState, Iterator[Event]):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._iterator = self._iterate()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A stream has only one reader")
        try:
            if self.closed:
                raise StopIteration
            event = next(self._iterator)
            if event.type != "ping":
                self.last_seq = event.seq
            return event
        finally:
            self._lock.release()

    def close(self):
        self.closed = True
        if self._response:
            self._response.close()
        if self._lock.acquire(blocking=False):
            try:
                self._iterator.close()
            finally:
                self._lock.release()

    def _iterate(self):
        while not self.closed:
            self._check()
            error = None
            try:
                with self.client._http.stream(**self._request()) as response:
                    self._response = response
                    if response.status_code != 200:
                        response.read()
                    self._status(response)
                    if self.contract_version == 2:
                        snapshot = bytearray()
                        for chunk in response.iter_bytes():
                            self._check()
                            if self.closed:
                                return
                            snapshot.extend(chunk)
                            if len(snapshot) > self.max_snapshot_bytes:
                                raise ProtocolError("Legacy replay snapshot exceeded size limit")
                        for historical in self._snapshot(bytes(snapshot)):
                            yield historical
                        return
                    decoder = SSEDecoder(self.max_frame_bytes)
                    for chunk in response.iter_bytes():
                        self._check()
                        if self.closed:
                            return
                        for raw in decoder.feed(chunk):
                            event = self._event(raw)
                            if event:
                                yield event
                    decoder.finish()
            except (httpx.TransportError, ReplayGapError, EnvaiError) as exc:
                error = exc
            finally:
                self._response = None
            if self.closed:
                return
            self._check()
            self.cancel.wait(self._retry(error))


class AsyncEventStream(StreamState, AsyncIterator[Event]):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._iterator = self._iterate()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A stream has only one reader")
        try:
            if self.closed:
                raise StopAsyncIteration
            event = await anext(self._iterator)
            if event.type != "ping":
                self.last_seq = event.seq
            return event
        finally:
            self._lock.release()

    async def aclose(self):
        self.closed = True
        if self._response:
            await self._response.aclose()
        if self._lock.acquire(blocking=False):
            try:
                await self._iterator.aclose()
            finally:
                self._lock.release()

    async def _iterate(self):
        while not self.closed:
            self._check()
            error = None
            try:
                async with self.client._http.stream(**self._request()) as response:
                    self._response = response
                    if response.status_code != 200:
                        await response.aread()
                    self._status(response)
                    if self.contract_version == 2:
                        snapshot = bytearray()
                        async for chunk in response.aiter_bytes():
                            self._check()
                            if self.closed:
                                return
                            snapshot.extend(chunk)
                            if len(snapshot) > self.max_snapshot_bytes:
                                raise ProtocolError("Legacy replay snapshot exceeded size limit")
                        for historical in self._snapshot(bytes(snapshot)):
                            yield historical
                        return
                    decoder = SSEDecoder(self.max_frame_bytes)
                    async for chunk in response.aiter_bytes():
                        self._check()
                        if self.closed:
                            return
                        for raw in decoder.feed(chunk):
                            event = self._event(raw)
                            if event:
                                yield event
                    decoder.finish()
            except (httpx.TransportError, ReplayGapError, EnvaiError) as exc:
                error = exc
            finally:
                self._response = None
            if self.closed:
                return
            self._check()
            await asyncio.sleep(self._retry(error))
