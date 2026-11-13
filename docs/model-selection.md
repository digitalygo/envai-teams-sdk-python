# Per-turn model selection

[README](../README.md) · [Configuration](getting-started.md) · [API reference](api-reference.md) · [Streaming](streaming.md) · [Chat attachments](chat-attachments.md)

This is unreleased source, verified with offline tests and fake providers only. The bundled contracts are fixed at `digitalygo/envai-teams` commit `f970714c0a358ff94b14f15eabde197c55861e74`. That is a source pin, not a claim about any deployed server.

## Usage

Pass `model` to pin the root orchestrator model for one turn. The keyword exists on the sync and async `send_and_stream` and `send_and_wait` helpers. Low-level `send_message` takes `model` in the body, either a dict or a `models.ChatMessageRequest`.

```python
from envai import EnvaiClient

with EnvaiClient(api_key, base_url="https://your-edge.example") as client:
    catalog = client.models.list()
    chosen = next(
        (entry.id for entry in catalog.data if getattr(entry, "type", None) == "llm" and entry.selectable),
        None,
    )
    if chosen is None:
        raise RuntimeError("No model reported as selectable")

    result = client.chats.send_and_wait(
        chat_id, "Summarize the project.", model=chosen, idempotency_key="summary-1"
    )
    follow_up = client.chats.send_and_wait(chat_id, "And the risks?")

    accepted = client.chats.send_message(
        chat_id, body={"message": "Draft a reply.", "model": chosen}
    )
```

`AsyncEnvaiClient` is identical with `await` and `async for`. Subsequent calls without `model` use the normal configured cascade.

## Rules

- Use an exact, case-sensitive `id` copied from an `llm` entry of `GET /models`. The SDK does not trim, fold case, translate aliases or prefetch the catalog. It keeps no client-side model list and applies no allowlist.
- `selectable` is guidance only. `selectable=False` is retained as received and the SDK does not block the request; the server decides.
- The model applies to the root of that one turn. It never changes team, agent, chat or default configuration and is not carried to the next call. Omit `model` to use the normal configured cascade.
- Leave `model` unset for no selection. The helper default is a sentinel, not `None`. Passing `model=None`, a non-string, an empty or a padded string is never treated as omission or a fallback. The SDK raises `ValidationError` before message submission, so no message POST is sent. Observation and read requests such as opening the event stream can precede validation. On a DTO, `ChatMessageRequest(message="hi")` leaves the field unset and `ChatMessageRequest(message="hi", model=None)` is an explicit null, which is rejected when sent.
- An explicit `model` needs no native tasks or `strict_model`. If you also pass `strict_model=True`, its existing capability gates (`native_tasks` and the advertised `strict_model` feature) still apply. With `strict_model` omitted, false or true, an explicit `model` is pinned exactly with no fallback.
- `attachment_ids` require `idempotency_key`. On a deliberate retry send the same `model`, `strict_model` and native policy with the same key. A changed or omitted `model` answers `409 idempotency_conflict`. The SDK never resends or falls back to another model on its own.
- Failures keep `http_status` and `code` on `EnvaiError`: `422` `invalid_model`, `unknown_model`, `model_unavailable`, `model_incompatible`, and `503` `model_catalog_unavailable`. There is no `model_not_selectable` error.
- `AgentStartedEvent` data `model` and the agent envelope `model` report the selected logical id. On `AgentDoneEvent`, `requested_model` (the logical request) and `model` (the provider-reported model) are separate fields and can differ. Their existing native-task gating is unchanged.
- Server restart or eviction loses the resident buffer with no paid rerun; completion terminal snapshot metadata is available only when recorded. Reconcile with the original key and policy.
- Only chat messages select a model. Jobs, delegation, deliveries and wiki endpoints have no model selection.
