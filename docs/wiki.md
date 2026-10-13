# Wiki V1 and provider usage

Both `EnvaiClient` and `AsyncEnvaiClient` expose the same `wiki` methods and typed Pydantic models. All authenticated Wiki calls select and require contract 3. Mutation preflight verifies the versioned capability route; mutation requests are sent once. A timeout can mean the server accepted the request. Keep the same idempotency key and reconcile status rather than automatically resubmitting paid work.

## Upload-first lifecycle

```python
from envai import EnvaiClient

with EnvaiClient(api_key, base_url="https://envai.example/api/v1") as client:
    source = client.wiki.upload_file(
        workspace_id, "report.pdf", idempotency_key="report-upload-2026-10"
    )
    # HTTP 202 means original acquired, conversion queued, indexing pending.
    source = client.wiki.source(workspace_id, source.file_id)
    if source.conversion.status == "completed":
        operation = client.wiki.inbox(
            workspace_id,
            body={"webhook_url": "https://application.example/wiki/events"},
            idempotency_key="report-inbox-2026-10",
        )
        operation = client.wiki.operation(workspace_id, operation.operation_id)
```

Use `await` with the identical methods on `AsyncEnvaiClient`. `upload_file` lazily streams a file, closes its owned handle on success/error/cancellation, and enforces the inclusive 1,000,000,000-byte limit without reading it into memory. The lower-level `wiki.upload(files={"file": (...)}, idempotency_key=...)` accepts bytes or a seekable file and also checks its size; user-owned handles remain yours. Supported originals are MD/TXT, CSV, XLSX, PDF/DOCX, MP3 and MP4. MP4 conversion processes audio only. No application duration limit or silent truncation is implied.

- `wiki.source(workspace_id, file_id)` returns typed lifecycle, monotonic `source_revision`, conversion/indexing, and current costs. Optional sanitized conversion `error_code`, `recoverable`, and `next_retry_at` describe an actually scheduled eligible retry. Missing old diagnostics remain `None`.
- Conversion success stages `inbox`; it does not index or emit a file-processed event. Explicit `wiki.inbox` starts indexing.
- `wiki.withdraw(workspace_id, body={"file_id": file_id})` immediately stages outbox and removes RAW visibility. It does not run cleanup or emit an operation event.
- Explicit `wiki.outbox(..., body={"webhook_url": ...}, idempotency_key=...)` cleans artifacts and originals. `wiki.sync` runs inbox and outbox sequentially; a failed first phase does not skip the second.
- `wiki.download(workspace_id, file_id)` returns original bytes through the current authenticated RAW visibility gate. A withdrawn source is no longer readable; no public storage URL is fabricated.
- Operation status is `queued|running|completed|failed`; per-phase/per-file outcomes, captured batch sizes and latest usage remain typed. Partial success is preserved.

An operation start reuses its original result only for the identical idempotency payload. A conflicting key returns 409; `429 wiki_queue_full` and `Retry-After` are hints, not reservations. Polling/status reads and webhook redelivery do not add provider invocations. Downloads return bytes; stream large original ingestion through `upload_file`.

## Exhaustive catalog, artifacts and revisions

```python
categories = client.wiki.categories(workspace_id)
index = client.wiki.category_index(workspace_id, category_id, all=True)
page = client.wiki.category_index(
    workspace_id, category_id, from_=1, to=25,
    type="raw", catalog_revision=categories.catalog_revision,
)
```

Choose exactly `all=True` or one-based inclusive `from_`/`to`. Omit `type` for both RAW and artifacts. Every GET is deterministic and makes no AI call. Ordering is insertion time then entry ID. The SDK rejects inconsistent counts and truncated exhaustive results, including through the raw operation method. Pass the returned `catalog_revision` on subsequent pages; 409 `catalog_revision_changed` means restart the read, never combine revisions. Old revisions do not grant access to withdrawn sources.

Decisions QUERY is probabilistic and internal. Public exhaustive GET covers entries QUERY may omit. Artifact creation/editing, CAS, reference requests and lifecycle confirmation are internal actor-bound V1 capabilities; this SDK does not invent public writer endpoints. Existing generic artifact/download APIs remain available with their existing scopes. Generic file paths cannot bypass managed Wiki namespaces.

## Consumption is independent of outcome

`billing.status` is `inactive`. `usage_cost` is tracked provider consumption, not a customer charge. `basis` is reported/calculated/mixed/none; `revision` and `as_of` identify a projection. `status=complete` requires a closed scope and no unpriced invocation. Pending scopes keep `totals=None`; `priced_subtotals` is only the known portion. A genuinely empty closed scope has zero invocations and empty totals/subtotals.

Amounts stay decimal strings in original currencies. For arithmetic, explicitly construct `decimal.Decimal(amount)` and group by currency. Do not add EUR to USD, apply implicit FX, round early or substitute zero for missing costs. A successful operation may have pending costs; a failed operation may have positive costs. Conversion consumption belongs to upload and is not added to inbox again; sync unions phase consumption.

ATP3 `usage.cost_usd`, terminal `cost`, and `chat_cost`, and completion `cost_usd`, are nullable. A legacy USD projection exists only for a complete USD-only tracked amount or a closed empty scope. Terminal snapshots and event bytes are immutable. Later GET costs may advance independently; no second functional terminal is emitted. Typed `Completion.terminal_snapshot` binds to that completion's turn and retains optional accepted-protocol provenance, optional chat totals, and unknown fields. It does not contain response text.

## Exact-byte outgoing webhooks

```python
from envai import WikiWebhookVerifier, SqliteWikiWebhookInbox, ReceiptOutcome

verifier = WikiWebhookVerifier.from_client(client, workspace_id)
inbox = SqliteWikiWebhookInbox("wiki-receipts.sqlite3")
inbox.create_schema()  # Explicit application setup/migration step.
outcome = inbox.receive(
    verifier, raw_request_body, request_headers,
    connection_scope="stable-envai-account-and-connection",
)
# Return 2xx only for ACCEPTED or DUPLICATE, after durable commit.
assert outcome in (ReceiptOutcome.ACCEPTED, ReceiptOutcome.DUPLICATE)
```

Read exact request bytes once. Pass a header collection that preserves duplicates, such as `httpx.Headers` or a list of pairs; a framework dictionary may already have discarded evidence of duplicates. Verify before parsing or reserializing. The domain-separated Ed25519 preimage is `envai-webhook-v1`, key ID, logical event ID, canonical delivery timestamp and lowercase SHA256 of the raw body, separated by LF with no trailing LF. Strict unpadded canonical base64url is required. Missing/repeated/malformed headers, wrong workspace/event identity, non-Ed25519 signing keys, invalid schemas, bad signatures and delivery timestamps outside ±300 seconds are rejected.

`from_client` fetches only configured Envai origin `/.well-known/wiki-webhook-jwks.json`, without bearer credentials or negotiation headers, and never follows redirects or payload URLs. Public keys cache for at most 300 seconds with one bounded refresh for an unknown cached key ID. Old events with fresh valid delivery signatures are allowed. The default body limit is 1 MiB; choose a deliberate explicit larger `max_body_bytes` for unusually large outcomes. The incoming ElevenLabs/provider HMAC protocol is separate and is never treated as a Wiki signature.

Processed/deleted events carry source revisions. Ignore stale revisions and retain deleted tombstones in application state; do not let a late processed event resurrect a deleted source. Re-read current source status before downstream reads/work. Terminal events carry immutable billing/usage projections; file events do not claim final costs.

## Durable application outbox

SQLite file-backed `FULL` transactions and PostgreSQL `synchronous_commit=on` both insert one scoped receipt and one application job atomically. PostgreSQL support is an optional dependency: `pip install 'envai-teams-sdk[postgres]'`, then `PostgresWikiWebhookInbox(conninfo)`. Database handles are owned per operation, never shared nested caller transactions. A verified event is deduplicated by connection scope + workspace + event ID; a different exact body for that identity returns `COLLISION`, which must not receive a positive acknowledgment.

`pending(limit=100)` returns typed jobs with exact bytes; `complete(job.id)` marks successful local processing. Job dispatch is at-least-once: several workers may see the same pending job. Use its stable job ID as downstream idempotency key or your own transactional claim. Remote side effects never run inside intake. A generic callback cannot provide exactly-once external effects. A crash before receipt/job commit leaves both absent; a crash after commit is safely replayed as a duplicate. `complete` is only appropriate after the downstream effect is durably idempotent.

For asynchronous servers, use `AsyncWikiWebhookVerifier` and `await inbox.receive_async(...)`, `pending_async()` and `complete_async()`. Explicit setup also has `create_schema_async()`. Database work runs in a separate thread with its own connection. Task cancellation can race a database commit; retrying the identical verified event remains safe. Verification failure/store failure raises and must not be acknowledged. No automatic provider requests, remote dispatch, credential provisioning or production migrations are performed.
