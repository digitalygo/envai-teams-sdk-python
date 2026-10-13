"""Envai Teams SDK: explicit clients, strict ATP events, opt-in client tools."""

from . import errors, models
from ._transport import RequestOptions, RetryPolicy
from .client import AsyncEnvaiClient, EnvaiClient
from .errors import CompatibilityError, WebhookStoreError, WebhookVerificationError
from .events import Event, TurnResult, UnknownEvent, parse_event
from .stream import AsyncEventStream, EventStream
from .wiki_inbox import (
    PostgresWikiWebhookInbox,
    ReceiptOutcome,
    SqliteWikiWebhookInbox,
    WikiOutboxJob,
)
from .wiki_webhooks import AsyncWikiWebhookVerifier, WikiWebhookVerifier

__version__ = "1.0.0a2"
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

__all__ += [
    "CompatibilityError",
    "WebhookStoreError",
    "WebhookVerificationError",
    "WikiWebhookVerifier",
    "AsyncWikiWebhookVerifier",
    "SqliteWikiWebhookInbox",
    "PostgresWikiWebhookInbox",
    "ReceiptOutcome",
    "WikiOutboxJob",
]
