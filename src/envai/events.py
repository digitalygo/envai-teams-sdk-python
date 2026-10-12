"""ATP v2 models, strict validation, and incremental SSE framing."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter

from . import models
from ._schema import EVENT_VALIDATOR, EVENTS, strict_json
from .errors import ProtocolError

TERMINAL_TYPES = frozenset(("turn.completed", "turn.cancelled", "turn.failed"))
PAYLOAD_TYPES = {
    key: getattr(models, "ATP" + "".join(x.title() for x in key.split(".")))
    for key in EVENTS["$defs"]
    if key not in ("Envelope", "Event", "Agent", "Usage", "ToolKind")
}


@dataclass(frozen=True)
class Event:
    v: int
    id: str
    seq: int
    ts: str
    chat_id: str
    turn_id: str | None
    agent: models.ATPAgent | None
    type: str
    data: Any = field(repr=False)
    raw: dict[str, Any] = field(repr=False)

    @property
    def terminal(self) -> bool:
        return self.type in TERMINAL_TYPES


@dataclass(frozen=True)
class UnknownEvent(Event):
    """Valid future envelope with the original unknown data unchanged."""


def parse_event(raw: str | bytes | dict[str, Any]) -> Event:
    original = raw
    try:
        value = strict_json(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(value, dict) or value.get("v") != 2:
            raise ProtocolError("Unsupported or missing ATP protocol version", raw=original)
        error = next(EVENT_VALIDATOR.iter_errors(value), None)
        if error:
            raise ProtocolError("Invalid ATP envelope or known payload", raw=original)
        timestamp = value["ts"]
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", timestamp
        ):
            raise ValueError("Timestamp must be RFC3339 with timezone")
        datetime.fromisoformat(timestamp.upper().replace("Z", "+00:00"))
        data = value["data"]
        from .event_models import EVENT_TYPES

        cls = EVENT_TYPES.get(value["type"], UnknownEvent)
        if cls is not UnknownEvent:
            data = TypeAdapter(PAYLOAD_TYPES[value["type"]]).validate_python(data)
        agent = models.ATPAgent.model_validate(value["agent"]) if value["agent"] else None
        return cls(
            **{key: value[key] for key in ("v", "id", "seq", "ts", "chat_id", "turn_id", "type")},
            agent=agent,
            data=data,
            raw=value,
        )
    except ProtocolError:
        raise
    except (ValueError, UnicodeError, KeyError, TypeError) as exc:
        raise ProtocolError("Malformed ATP JSON", raw=original) from exc


class SSEDecoder:
    """WHATWG SSE data framing, with bounded memory and strict UTF-8.

    Comments and unknown fields are ignored. IDs never override ATP seq.
    Incomplete EOF frames are discarded, including a final unterminated line.
    """

    def __init__(self, max_frame_bytes: int = 2 * 1024 * 1024):
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self._line: list[str] = []
        self._data: list[str] = []
        self._cr = False
        self._size = 0
        self.max_frame_bytes = max_frame_bytes

    def feed(self, chunk: bytes) -> list[str]:
        try:
            text = self._decoder.decode(chunk)
        except UnicodeError as exc:
            raise ProtocolError("SSE is not valid UTF-8") from exc
        result = []
        for char in text:
            if self._cr:
                self._cr = False
                if char == "\n":
                    continue
            self._size += len(char.encode("utf-8"))
            if self._size > self.max_frame_bytes:
                raise ProtocolError("SSE frame exceeded size limit")
            if char in "\r\n":
                self._cr = char == "\r"
                line, self._line = "".join(self._line), []
                if not line:
                    if self._data:
                        result.append("\n".join(self._data))
                    self._data = []
                    self._size = 0
                elif line.startswith("data:"):
                    value = line[5:]
                    self._data.append(value[1:] if value.startswith(" ") else value)
                elif line == "data":
                    self._data.append("")
            else:
                self._line.append(char)
        return result

    def finish(self) -> None:
        try:
            self._decoder.decode(b"", final=True)
        except UnicodeError as exc:
            raise ProtocolError("Truncated UTF-8 SSE stream") from exc
        self._data.clear()
        self._line = []


@dataclass(frozen=True)
class TurnResult:
    chat_id: str
    turn_id: str
    text: str
    terminal_event: Event
    last_seq: int

    @property
    def usage(self) -> models.ATPUsage:
        return self.terminal_event.data.usage

    @property
    def cost(self) -> Any:
        return self.terminal_event.raw["data"].get("cost")


class TurnAccumulator:
    def __init__(self):
        self.text = ""
        self._root: str | None = None

    def accept(self, event: Event) -> None:
        if event.agent and event.agent.role == "orchestrator":
            if self._root is None:
                self._root = event.agent.instance_id
            if event.agent.instance_id == self._root:
                if event.type == "agent.text.delta":
                    self.text += event.data.delta
                elif event.type == "agent.text.done":
                    self.text = event.data.text
