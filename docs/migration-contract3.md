# Migration to contract 3

This is a breaking client/server negotiation change. Upgrade server routes and schema support before using the default new client. Publishing an SDK does not deploy the backend or FFS. While those changes remain in review, default new-contract requests intentionally fail closed.

1. `EnvaiClient` and `AsyncEnvaiClient` now select `X-Envai-Contract-Version: 3` by default. Base URLs ending in `/api/v1` remain valid. ATP uses `/api/v1/atp/v3/...` and verifies acknowledged protocol3 capabilities before every mutation. Successful negotiated responses must return exactly one matching header before decoding. Old-server404, unsupported contracts and missing/mismatched acknowledgment raise `CompatibilityError`. There is no automatic downgrade or fallback to an unversioned mutation route.
2. Apply nullable handling to ATP usage and terminal USD costs and completion USD totals. Prefer typed `usage_cost`; preserve decimal currency strings and pending state. Numeric historical values retain their meaning. Unknown values never become zero.
3. Contract3 streams accept exact valid historical ATP2 events and current ATP3 events; they do not rewrite versions, IDs, sequences or raw payloads. Reconnects keep the selected route/header. `parse_event(..., contract_version=2)` rejects ATP3 and any null legacy cost. The existing client-tool signature preimage is unchanged.
4. Explicit `contract_version=2` is read-only. All mutations and authenticated Wiki methods fail locally. `get_capabilities_legacy` and `stream_chat_events_legacy` additionally require that explicit mode and cannot silently bypass default3. Legacy capabilities must be protocol2 and acknowledge2. Legacy completion reads validate the numeric original contract before hydration. A page with any unrepresentable item fails without exposing a partial page.
5. Legacy replay is a finite snapshot, not a live subscription. The SDK buffers and validates the whole selected snapshot before yielding any event. It does not reconnect after a successful snapshot. Memory is bounded by `max_snapshot_bytes` (default64MiB); an oversized or partial snapshot fails explicitly without exposing a prefix. V3 keeps existing single-reader streaming and cancellation/replay-gap behavior.
6. Mutation retries remain disabled, including when a successful response lost its acknowledgment. Capability preflight may safely retry GET under the configured policy. An ambiguous mutation must be reconciled rather than resubmitted. Wiki starts/uploads take explicit stable idempotency keys.
7. Completion metadata adds immutable `terminal_snapshot` alongside the current usage projection. Terminal ID/time/data stay unchanged on late reconciliation; optional `v` and `accepted_protocol_version` retain recovery provenance. Snapshot metadata is not response-text replay.
8. Public Wiki/JWKS, lifecycle/cost models, conversion diagnostics, exact-byte Ed25519 verification and durable receipt/application outbox are new. See [Wiki integration](wiki.md). Public artifact writers/Decisions QUERY/CAS are not introduced because those capabilities are internal in V1.

Unrelated control-plane CRUD retains existing routes and does not require acknowledgment headers. Public JWKS and incoming provider HMAC callbacks are unnegotiated. Forward capability feature strings are accepted; capability features alone never establish contract compatibility. Never override the SDK selection by adding request headers.

The source tree retains immutable original OpenAPI2 and strict numeric ATP2 schemas. The original baseline fixtures/provenance remain unchanged; the coordinated contract3 source manifest is separately tracked. Normal release automation owns package versions and tags. Do not rewrite earlier tags or assume a previously published alpha package includes this migration.

## Chat attachments and deferred Wiki outcomes

Update to the matching backend before enabling attachment sends. Contract 3 remains selected, and `chat_attachments` is an additional capability requirement. Preserve message idempotency keys and ordered IDs; verify acceptance. Handle the new `deferred` Wiki file outcome. See [attachment integration](chat-attachments.md).

## Project-owned resources

Current resources require project ownership fields. Historical completion reads hydrate `models.LegacyCompletion` or `models.LegacyCompletionCollection` under explicit contract2; get/list return annotations include those shapes, keeping v3 requirements strict. Current completions require `project_id` and `api_key_id`, with nullable `user_id` audit provenance. For workspace/chat/agent/team ownership, replace `user_id` with `project_id`; nullable `created_by_user_id` is audit provenance only. Workspace metadata accepts objects and lists, including the server default `[]`.

Tool registrations and deprecated UserTool aliases now use project scope. API-key management is session-only and fails locally in bearer clients. See the [API reference](api-reference.md).

## Project resources, crons, skills and reports (unreleased)

This source adds 14 operations on top of the `1.0.0a6` baseline (78 to 92 operations; 89 callable plus 3 unsupported API-key stubs), pinned to backend commit `ba021aa61e30e379880f809d45f90e5d3564d43e`. They are additive and need no negotiation change, but they are not in any published package until a release is cut from this source.

- HTTP 402 now raises `PaymentRequiredError` instead of a generic `EnvaiError`. It is a subclass, so broad `except EnvaiError` handlers keep working; narrow `except` clauses on other classes may need an update. Other mappings are unchanged: 503 `provider_error` is `ProviderError`, 503 `file_*` is `FileError`, and 503 `provider_configuration_error` is `ServerError`.
- `chats.update` sends only `title`; other keys fail locally with `ValidationError`.
- `workspaces.delete` and `chats.delete` return `CleanupResult` with pending cleanup (HTTP 200), not a purge confirmation.
- Use `client.project_reports`, `client.chat_crons` and `client.project_skills` for the new groups. See the [API reference](api-reference.md#project-resources).
