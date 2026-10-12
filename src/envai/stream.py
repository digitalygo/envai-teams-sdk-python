"""Single-reader, cancellable, best-effort resident-chat replay streams."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, AsyncIterator, Iterator
from urllib.parse import quote

import httpx

from ._schema import strict_json
from ._transport import delay
from .errors import (
    CancelledError,
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
    ):
        if after < 0 or max_reconnects < 0 or (max_duration is not None and max_duration <= 0):
            raise ValueError("Invalid stream bounds")
        if not chat_id or chat_id in (".", ".."):
            raise ValueError("chat_id must be a nonempty opaque identifier")
        self.client, self.chat_id = client, chat_id
        self.last_seq = after
        self.max_reconnects, self.max_duration = max_reconnects, max_duration
        self.cancel = cancel or threading.Event()
        self.max_frame_bytes = max_frame_bytes
        self.closed = False
        self.live = False
        self.reconnects = 0
        self._started = time.monotonic()
        self._response: httpx.Response | None = None
        self._gap_pending = False
        self._lock = threading.Lock()

    def _check(self):
        if self.cancel.is_set():
            raise CancelledError("Stream cancelled locally")
        if self.max_duration is not None and time.monotonic() - self._started > self.max_duration:
            raise TransportError("Stream duration limit reached")

    def _event(self, raw: str) -> Event | None:
        event = parse_event(raw)
        if event.chat_id != self.chat_id:
            raise ProtocolError("SSE event belongs to a different chat", raw=event.raw)
        if event.type == "ping":
            self.live = True
            self.client._observe("stream.live", last_seq=self.last_seq)
            return event
        if event.seq <= self.last_seq:
            self.client._observe("stream.duplicate", seq=event.seq)
            return None
        if event.seq != self.last_seq + 1:
            raise ReplayGapError(self.last_seq + 1, event.seq)
        self._gap_pending = False
        return event

    def _request(self):
        headers = {**self.client._headers, "Accept": "text/event-stream"}
        return dict(
            method="GET",
            url=self.client.base_url + "/chats/" + quote(self.chat_id, safe="") + "/events",
            params={"after": self.last_seq},
            headers=headers,
            timeout=self.client.stream_timeout,
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
            if exc:
                raise exc
            raise TransportError("SSE reconnect limit reached")
        self.reconnects += 1
        self.live = False
        self.client._observe("stream.reconnect", attempt=self.reconnects, after=self.last_seq)
        return (
            0 if isinstance(exc, ReplayGapError) else delay(self.client.retry, self.reconnects - 1)
        )

    def _status(self, response: httpx.Response):
        if response.status_code != 200:
            try:
                payload = strict_json(response.content)
            except (ValueError, UnicodeError):
                payload = None
            raise from_response(response.status_code, payload, response.headers.get("x-request-id"))
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
