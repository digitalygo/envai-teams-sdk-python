# Streaming and recovery

[README](../README.md) · [Configuration](getting-started.md) · [API reference](api-reference.md) · [Client tools](client-tools.md)

## Sending a turn safely

`client.chats.send_and_stream(chat_id, message, timeout=120)` waits for the first live `ping` before sending once, then yields only the accepted turn's events. Its async counterpart is an async generator. The HTTP stream response alone does not prove live readiness. A timeout before readiness prevents message submission.

`send_and_wait` aggregates top-level orchestrator text and returns a `TurnResult`: `chat_id`, `turn_id`, `text`, `terminal_event`, `last_seq`, and usage/cost properties. A final text event replaces accumulated deltas. Failed and cancelled terminals remain explicit; check `terminal_event.type` even when no exception was raised. Only `turn.completed`, `turn.cancelled`, and `turn.failed` are turn terminals.

Always close a generator when stopping early (`close` or `aclose`), and use client/stream context managers. See the [sync streaming example](../examples/stream_turn.py) and [async tool example](../examples/client_tools.py).

## Low-level streams

`client.chats.events(chat_id, after=0, max_reconnects=5, max_duration=None, cancel=None, max_frame_bytes=2097152)` gives explicit control. A stream has one reader and exposes `last_seq`, `live`, and `reconnects`. The parser handles fragmented SSE and validates the full ATP envelope and known payloads.

`ping` uses sequence zero and does not advance the cursor. Duplicates are suppressed; an observable upward gap attempts replay once, then raises `ReplayGapError` if still unresolved. Reconnects are bounded and cancellable; fatal authentication/protocol errors stop immediately. Save a durable application checkpoint only after your processing commits. The diagnostic cursor alone does not make your side effects transactional.

Sync cancellation uses a `threading.Event` and is checked during reads/backoff; idle reads are bounded by `stream_read_timeout`. Async cancellation also follows normal task cancellation. Closing a stream stops local work only. To cancel a remote turn, explicitly call `client.chats.cancel(chat_id)` and observe `turn.cancelled`.

## Resume limits

Replay is best-effort within the same resident chat session. After restart/eviction, sequences may reset; there is no durable epoch or cursor-expired response. A lost prefix or reset may be invisible to gap detection. After a known reset, reconcile application state before reusing a checkpoint. A new stream does not establish complete history.

Replay and live subscription are not atomic; ping-before-send reduces the new-turn attach race but cannot recover already missing events. All subscribers can see the same client-tool calls. Use one executor or a shared atomic durable store when multiple processes may act on them.

## Handling failures

- Authentication/access errors: fix account access or credentials rather than retrying unchanged.
- `ValidationError`: correct the request; avoid logging sensitive `details` indiscriminately.
- `ProtocolError` / `ReplayGapError`: preserve safe diagnostics and reconcile state; do not treat EOF before a terminal as success.
- `TransportError` / local cancellation after a mutation: the remote result may be ambiguous. Mutations are never automatically retried.
- `ToolStateError`: preserve records and reconcile effects before repair; corruption is not an absent claim.

Normal GET requests retry transient transport failures and 429/502/503/504 under `RetryPolicy`; streams use a separate reconnect budget. An output-delivery timeout, 404, or 409 does not prove which output was accepted. Observe the matching result and check authoritative downstream state.

Approval resolution currently returns 409. A steer acknowledgement does not guarantee changed execution. Restore does not start a turn and its acknowledgement does not guarantee durable history. Fork requires an empty destination chat with matching owner/workspace and both chats idle. These API limitations are not resolved by client retries.
