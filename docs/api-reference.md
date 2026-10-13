# Python API reference

[README](../README.md) · [All 52 operations](api-inventory.md) · [Streaming](streaming.md) · [Client tools](client-tools.md)

## Services and primitives

`EnvaiClient` and `AsyncEnvaiClient` expose the same operation names. Await async methods. Service groups are `workspaces`, `tools`, `chats`, `completions`, `files`, `agents`, `teams`, `models`, `api_keys`, `images`, and `capabilities`.

Every contract operation also has a snake_case primitive directly on the client: `sendChatMessage` becomes `send_chat_message`. Inspect [generated signatures](../src/envai/_generated.py) for exact positional/keyword arguments and return annotations. [The inventory](api-inventory.md) lists HTTP paths and operation IDs; paths are relative to `/api/v1` except origin-level JWKS.

Use generated request types from `envai.models`, or supported JSON dictionaries where a method accepts them. API field names stay snake_case. A missing optional field is omitted from JSON; explicit null is allowed only for nullable fields. API-key expiry omission means the server default, whereas `expires_at="never"` explicitly requests no expiry. Never log the returned plaintext key.

Resource and ordinary collection `data` wrappers are unwrapped into typed results. Completion pages retain `data`, `links`, and `meta`. IDs are strings. Monetary/usage numbers preserve the wire representation; the SDK does not derive billing from local price tables.

## Pagination and files

`completions.iterate(workspace_id, chat_id)` yields items and follows numeric pages on the configured origin. It never follows a response-supplied URL. This is a Python convenience extension: the bundled OpenAPI response is paginated but its request omits `page`. Deployments must support numeric paging; use the primitive page result when that compatibility is uncertain. The iterator does not use the normal GET retry wrapper.

Multipart inputs use HTTPX-style file tuples. Opening local files is an application decision. Binary download methods return `bytes`; they do not write files automatically, and large downloads require enough memory for the returned content.

`images.download_signed_url(url)` validates the configured origin and artifact endpoint, preserves signed query bytes/order, and omits bearer credentials. The primitive accepts opaque `artifact`, `generation`, `signature`, and optional `expires` fields. Do not replace these with local paths or log signed URLs. Legacy `path` artifact URLs are unsupported.

`images.generate_and_wait` polls the generation until a terminal status or timeout; inspect the final status. A polling timeout does not prove a remote generation was cancelled. Provider-webhook and image-test primitives are exposed for contract completeness, not automatically called in normal workflows.

## Events and errors

Typed event classes live in `envai.event_models`. Known events are checked against ATP v2 and their payload schemas; malformed known events raise `ProtocolError`. A valid additive type becomes `UnknownEvent` with original content preserved. Capabilities discovery is not a parser allowlist.

`EnvaiError` and subclasses expose `http_status`, `type`, `code`, `message`, `details`, and `request_id`. Branch on code/status. The exception string excludes potentially sensitive server details; explicit properties and `ProtocolError.raw` may still contain private content.

[Transport configuration](getting-started.md#client-configuration) and [stream recovery](streaming.md) describe timeout and retry behavior. Mutation methods are sent once. Generated models and operation coverage do not imply every server feature is currently available; preserve 404/409/validation responses rather than treating them as success.
