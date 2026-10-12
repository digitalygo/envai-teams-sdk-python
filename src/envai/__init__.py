"""Envai Teams SDK: explicit clients, strict ATP events, opt-in client tools."""

from . import errors, models
from ._transport import RequestOptions, RetryPolicy
from .client import AsyncEnvaiClient, EnvaiClient
from .events import Event, TurnResult, UnknownEvent, parse_event
from .stream import AsyncEventStream, EventStream

__version__ = "0.1.0a1"
__all__ = [
    "EnvaiClient",
    "AsyncEnvaiClient",
    "RetryPolicy",
    "RequestOptions",
    "Event",
    "UnknownEvent",
    "TurnResult",
    "parse_event",
    "EventStream",
    "AsyncEventStream",
    "models",
    "errors",
]
from .tool_state import MemoryToolState, SQLiteToolState, ToolStateStore
from .tool_verification import AsyncJwksVerifier, JwksVerifier
from .tools import AsyncClientToolRunner, ClientToolRegistry, ClientToolRunner, ToolContext

__all__ += [
    "ClientToolRegistry",
    "ClientToolRunner",
    "AsyncClientToolRunner",
    "ToolContext",
    "MemoryToolState",
    "SQLiteToolState",
    "ToolStateStore",
    "JwksVerifier",
    "AsyncJwksVerifier",
]
