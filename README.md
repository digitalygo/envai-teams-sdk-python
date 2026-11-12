# Envai Teams Python SDK

Typed synchronous and asynchronous clients for the current Envai Teams API and
Agent Team Protocol (ATP3 with strict historical ATP2 replay). Python 3.11+. The package baseline in this
source tree is the **alpha prerelease** `1.0.0a6`; the source on this branch is **unreleased** and asserts no new published version.
Prerelease APIs may change; pin the version and review upgrades before production use.

- [Installation and configuration](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/docs/getting-started.md)
- [HTTP API and models](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/docs/api-reference.md) · [reviewed 92-operation subset](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/docs/api-inventory.md)
- [Streaming and error recovery](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/docs/streaming.md)
- [Signed client tools and deployment](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/docs/client-tools.md)

## Package installation

```sh
python -m venv .venv
# Activate .venv using the command for your shell, then:
python -m pip install "envai-teams-sdk==1.0.0a6"
```

**Compatibility note:** the `1.0.0a6` baseline above already contains the contract3 migration. This branch adds project resources, crons, skills, project reports and `PaymentRequiredError` on top of it. They are not in `1.0.0a6`; use the release produced from this branch, or the documented source install while reviewing.

The explicit version selects the alpha prerelease; no `--pre` flag is needed with this exact pin. The distribution is `envai-teams-sdk`; import `envai`. GitHub release tag `v1.0.0-alpha.6` uses Python package version `1.0.0a6`. The baseline tag is an unreleased repository baseline; publication to PyPI is not claimed unless independently verified on the index. For source installation and development, see [getting started](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/docs/getting-started.md).

HTTP uses HTTPX, models use
Pydantic, schema validation uses the pinned JSON Schema, and client-tool signatures
use `cryptography` Ed25519 plus the `rfc8785` implementation. No generated code or
schema validator downloads remote references at runtime.

## Quick start

```python
import os
from envai import EnvaiClient, models

with EnvaiClient(os.environ['ENVAI_API_KEY'], base_url=os.environ['ENVAI_BASE_URL']) as client:
    workspace = client.workspaces.create(body=models.WorkspaceCreateRequest(name='Research'))
    # Choose a team already configured for this account.
    chat = client.chats.create(workspace.id, body=models.ChatCreateRequest(team_id=os.environ['ENVAI_TEAM_ID']))
    result = client.chats.send_and_wait(chat.id, 'Summarize the project', timeout=120)
    print(result.text)
    print(result.turn_id, result.last_seq, result.terminal_event.type)
    if result.terminal_event.type != "turn.completed":
        raise RuntimeError(f"Turn ended with {result.terminal_event.type}")
```

The quickstart creates remote resources and starts a potentially billable turn. Set `ENVAI_BASE_URL`, `ENVAI_API_KEY`, and an existing `ENVAI_TEAM_ID`. See the complete [quickstart](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/examples/quickstart.py), [stream example](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/examples/stream_turn.py), and [tool example](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/examples/client_tools.py).

No credentials are read implicitly or printed in client representations. Configure
an API key explicitly. The example reads an environment variable only in application
code. IDs are opaque strings, never UUID-coerced.

## Async and individual events

```python
from envai import AsyncEnvaiClient
from envai.event_models import AgentTextDeltaEvent

async def run(api_key: str, chat_id: str):
    async with AsyncEnvaiClient(api_key, base_url='https://your-edge.example') as client:
        async for event in client.chats.send_and_stream(chat_id, 'Hello', timeout=120):
            if isinstance(event, AgentTextDeltaEvent) and event.agent and event.agent.role == 'orchestrator':
                print(event.data.delta, end='')
            if event.terminal:
                print(event.type)
```

`send_and_stream` opens SSE and waits for the first **ping before sending once**.
An HTTP 200 stream response alone does not establish live readiness. Waiting for a ping may take one heartbeat interval.
Only matching `turn_id` events are returned; `agent.done` is not a turn terminal.
`send_and_wait` aggregates only the orchestrator's text. Failed/cancelled terminal
events remain explicit in `TurnResult.terminal_event`; they are not turned into
successful text-only results.

For existing turns or explicit lifecycle control, use `client.chats.events(chat_id,
after=checkpoint)` and `client.chats.send_message(...)` separately. Streams are
context managers, have one reader, expose `last_seq`, `live`, and `reconnects`, and
support `max_reconnects`, `max_duration`, `max_frame_bytes` and a `threading.Event`
`cancel`. Async streams also support ordinary task cancellation. Closing an iterator
releases the HTTP response. Sync cancellation is cooperative at reads/backoff;
`stream_read_timeout` bounds an idle read. Remote `chats.cancel` is a separate,
explicit mutation; closing a local stream never cancels a remote turn.

## API surface

Services: `workspaces`, `tools`, `chats`, `chat_crons`, `project_skills`,
`project_reports`, `completions`, `files`, `agents`, `teams`,
`models`, `api_keys`, `images`, `capabilities`. Resources and ordinary collections
are unwrapped from `data`; completion, usage and project log pages preserve `data`, `links`, and `meta`.
`completions.iterate(workspace_id, chat_id)` follows numeric pages on the configured
origin, never a response-supplied URL.

The generated primitive layer retains the reviewed **92 operation IDs** (89 callable plus 3 unsupported API-key stubs, from a 146-operation server contract) in
`src/envai/_contracts/operations.json`; every primitive has a snake_case method
(e.g. `send_chat_message` for `sendChatMessage`) in both clients. See
[the generated API inventory](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/docs/api-inventory.md). Health uses `/api/v1/up`;
JWKS is public and defaults to the **origin** `/.well-known/jwks.json`.
`jwks_url` can be overridden. The provider-webhook and local image-test primitives
are included for inventory completeness and are **experimental integration
surfaces**, not normal application workflows.

Generated request and response models are in `envai.models`; 39 typed ATP event
classes and their payloads are in `envai.event_models` / `envai.models`.
Known events are validated strictly against the current contract. A malformed
known event raises `ProtocolError` with the original data in `.raw`; a valid
future type returns `UnknownEvent` with data unchanged. Unknown capability names
are governed by the pinned HTTP contract, while unknown valid ATP event types are
accepted independently of the capabilities discovery list.

Optional request fields preserve omission. In particular, `ApiKeyCreate(name='x')`
omits `expires_at`; only explicit `expires_at='never'` means no expiration.
Never log the plaintext token returned by key creation. Nullable legacy USD cost numbers retain
floating-point precision from the wire without rounding to cents or estimating
billing from local price tables.

Multipart methods accept HTTPX-style file tuples. Binary methods return `bytes`;
no download is automatically written to disk. For a returned artifact URL use
`images.download_signed_url(url)`: it preserves query bytes/order, allows only the
configured origin and current `/api/v1/artifacts/download` path, and never sends
Bearer authentication. The raw artifact primitive uses the current
`artifact`, `expires`, `generation`, `signature` query fields in canonical alphabetical
order. Legacy `path=` artifact URLs are intentionally unsupported.

## Transport policy and observability

Both clients accept `timeout`, `connect_timeout`, `stream_read_timeout`, `retry`,
`transport`, `observer`, `base_url`, and `jwks_url`. An injected HTTPX transport
supports tests, proxies, custom TLS and application-specific routing. Environment
proxy discovery is disabled to avoid silently routing secrets through process-wide
proxy settings. Use a configured transport when a proxy is desired.

Only GET requests are automatically retried on transport failure or 429/502/503/504,
with bounded exponential jitter and bounded `Retry-After`. POST, PUT and DELETE
are never automatically retried, even when a status or network failure looks
transient. Per-call `RequestOptions` supports timeout and cooperative cancellation.
A mutation with an ambiguous network result must be reconciled by the application.
Redirects are not followed, including same-origin redirects.

`observer(name, details)` receives small structural events such as `http.retry`,
`stream.reconnect`, `stream.gap`, `stream.duplicate`, and `tool.error`. It never
receives credentials, URLs, tool arguments, output, or response bodies. Observer
exceptions are isolated from delivery behavior. Typed HTTP exceptions expose
`http_status`, `type`, `code`, `message`, `details`, and `request_id`; their string
representation deliberately omits potentially sensitive server messages/details.

## Opt-in client tools

No local tools, filesystem access, shell commands, or handlers are installed by
default. Server registration (`client.tools.create`) and agent authorization
(`external_tools_enabled=[{'name': 'your_tool'}]`) are separate from local handler
registration. Both are required for a server-visible client tool.

See [examples/client_tools.py](https://github.com/digitalygo/envai-teams-sdk-python/blob/alpha/examples/client_tools.py) for a complete bounded
async runner. The runner requires explicit deployment/account `scope`, `chat_id`,
`turn_id`, and a state store:

```python
from envai import ClientToolRegistry, SQLiteToolState, AsyncClientToolRunner

registry = ClientToolRegistry()
async def echo(args, context):
    context.check_cancelled()
    return {'echo': args['text']}
registry.register('echo', echo,
    {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']},
    timeout=120)

# Explicitly select a persistent database path. Nothing is created implicitly.
state = SQLiteToolState('/application-owned-data/envai-claims.sqlite3')
runner = AsyncClientToolRunner(client, registry, store=state,
    scope='production/account-id', chat_id=chat_id, turn_id=accepted_turn_id,
    max_concurrency=4)
```

Every request is snapshotted, validated, canonicalized with RFC 8785 and verified
with Ed25519 **before any handler runs**. Integers outside the safe exact
range (±(2^53−1)) are normalized to binary64 before both verification and
handler execution, matching the server and JavaScript numeric domain. Overflow
and non-finite values are rejected. Use strings when exact huge integers matter. JWKS caches have configurable TTL and one
refresh on unknown kid/verification failure. Unsupported algorithms and invalid
signatures fail closed. Locally registered argument schemas are cloned and cannot
retrieve remote references. An explicit `$schema` supports Draft 3, 4, 6, 7, 2019-09, or 2020-12 through the installed `jsonschema` validator. An omitted dialect defaults to Draft 2020-12; unknown dialects are rejected. Prefer a modern declared dialect for shared tools; TypeScript supports Draft 6 and newer. Literal `const`/`enum` values are preserved as data. Unknown/invalid local tools produce controlled error
outputs. Structured results are JSON-serialized exactly once; outputs are capped
at 128 KiB. Empty strings become a controlled error because the current API
rejects them despite the OpenAPI schema lacking `minLength`.

`ToolStateStore` is a pluggable protocol. Its atomic `claim` must commit before
returning true, and claims must not automatically expire. SQLite uses atomic
insertion and FULL synchronous commits. The key is `(scope, chat_id, call_id)`;
use a stable, non-secret deployment/account scope, and preserve the database.
`MemoryToolState()` is an explicit ephemeral option only. Stores retain hashes and
status, never arguments/outputs/credentials. A conflicting payload for the same
claim is rejected. Results observed before requests create resolved tombstones.
Both built-in stores reject malformed existing records before reading or changing
them; corruption is never treated as a missing claim or automatically repaired.
A status transition also rejects a missing claim, preventing output delivery if
local state disappears during handler execution.
This protects against pre-existing local state damage, not malicious access to a
database that an attacker can rewrite or delete. Resolve corrupt state explicitly
without discarding the deduplication history.

Statuses are `running`, `ambiguous`, `submitted`, and `resolved`. The runner stores
`ambiguous` **before POSTing output**, marks `submitted` only on HTTP success, and
marks `resolved` on the corresponding stream result. It never retries delivery or
reexecutes a duplicate call; a later 404/409 does not prove earlier success. Inspect
`.get(...)` and reconcile ambiguous states externally. A crash between side effect
and durable completion cannot be made exactly-once by an SDK: handlers must use
`context.call_id` as an idempotency key, transactions, or explicit reconciliation.
A signature authenticates the call ID, tool name, and canonical arguments. It does not sign the chat, turn, account, timestamp, or freshness. Only feed the runner trusted authenticated stream events for its configured turn.

Concurrency is bounded with no unbounded handler queue. Saturation returns a
controlled error. Handler deadlines use a conservative margin below the registered
timeout, since the server timer starts before delivery and no absolute deadline
is transmitted. Observed tool result/turn terminal cancels local work. Async runners
require native async handlers; sync runners use worker threads and cooperative
`ToolContext` cancellation. Python cannot forcibly terminate arbitrary threads:
a timed-out sync handler retains its concurrency slot until it exits, and must
check cancellation before every effect. Runner lifecycle is bound to one known
turn; it does not infer permission to execute unrelated turns.

## Current alpha limitations

- Replay is only best-effort while the same chat runtime stays resident. The
  sequence can reset after restart/eviction, live events can be lost, and the API supplies no epoch/cursor-expired signal.
  Upward gaps trigger one replay attempt, then `ReplayGapError`; the SDK cannot
  detect every reset or promise durable replay. Do not silently reuse a checkpoint
  after a known restart. Reconcile or explicitly start a new observation epoch.
- Replay/subscribe is not atomic. The first-ping workaround reduces the known
  attach race; it is not a server-level durability guarantee.
- A successful `steer` acknowledgement does not guarantee changed execution. Approval resolution currently returns HTTP 409 and is unavailable as a completed workflow. Neither operation is automatically invoked.
- Context persistence/rehydration is best-effort and may lose history; the SDK
  cannot assert durable context restore from a successful acknowledgment.
- A second tool output is 409 while the turn is active and may be 404 after cleanup.
  Neither response alone reconciles an ambiguous earlier delivery.

## Development and contract provenance

The bundled contract3 HTTP and ATP contracts include strict immutable numeric ATP2 replay schemas. Default clients select contract3 and fail closed against older servers. See [migration](docs/migration-contract3.md) and [Wiki V1](docs/wiki.md).
`tests/fixtures/contract3-provenance.json` records the six current shared contract snapshots, pinned to backend commit `ba021aa61e30e379880f809d45f90e5d3564d43e` of `digitalygo/1-1-5-envai-aaas`. The immutable baseline `tests/fixtures/provenance.json` separately records two historical byte-exact snapshots
from five SDK-authored fixtures. Guards verify the source commit/path, byte count,
SHA-256, and Git blob identity. Public guides are SDK documentation rather than
copied contract files. Building or reading the SDK does not require access to a
separate source repository.

```sh
python scripts/generate.py --check
ruff check src scripts tests examples
mypy src
pytest --cov=envai --cov-report=term-missing
python -m build
```

Regenerate only the typed models and REST primitives with
`python scripts/generate.py`; SSE/tool behavior is handwritten. CI runs Python
3.11–3.13, lint, type checks, contract drift checks, wheel build/import, all mocked
HTTP operations, shared ATP/SSE/JCS/Ed25519 vectors and concurrency/replay/error
regressions. Combined runtime line/branch coverage is gated at 90%; generated models/primitives
are excluded from coverage but independently covered by the operation inventory
and response-schema tests. Tests never call Envai production endpoints or require
credentials. MIT, Copyright (c) 2026 DigItalyGo.

Wiki V1 and contract3 validation results, database crash/concurrency scope and release limits: [validation report](docs/wiki-validation.md).

See [chat attachments](docs/chat-attachments.md) for upload, conversion, retry and streaming with files. This surface is unreleased until the matching backend is deployed.

## Native background tasks and delegated jobs

Canonical schemas and byte provenance are pinned to backend commit
`ba021aa61e30e379880f809d45f90e5d3564d43e` of `digitalygo/1-1-5-envai-aaas`. Earlier
native-task reviews targeted Envai server `964c11fe72887329e8d06c2afb483442d643b290`;
that commit is historical and no longer the pin.

Opt in explicitly with `EnvaiClient(api_key, native_tasks=True)` (or
`AsyncEnvaiClient`). This sends `X-Envai-Extensions: native-tasks-v1` on
capability discovery, mutations, SSE/reconnect and tool results. Native ATP
responses must acknowledge that extension before hydration or stream dispatch; a
missing mutation acknowledgment leaves an uncertain outcome and is not retried. The default
client retains the server's existing foreground behavior. Native helpers fail
closed without this opt-in.

Contract 3 clients negotiate `detached_background`, `native_deliveries`,
`cross_team_delegation`, `delegated_job_lifecycle`, `phase_classification`,
`strict_model`, and `delegation_notify_origin` before the corresponding request.
Unknown capability families and feature names are additive. These APIs also have
async counterparts and generated typed request/result models in `envai.models`:

```python
receipt = client.chats.delegate(origin_chat, body=models.TeamDelegationRequest(
    workspace_id=workspace_id, event_id=stable_event_id,
    origin_turn_id=origin_turn_id, target_chat_id=target_chat,
    prompt="Complete this phase", notify_origin=False, strict_model=True,
))
# Keep the target chat's tool stream running concurrently; see the example below.
final = client.chats.wait_for_task(origin_chat, receipt.task_id, timeout=300)
if final.status == "completed":
    print(final.output, final.result_turn_id)
else:
    print(final.status, final.error)  # failed/cancelled remain explicit outcomes
```

`receipt.turn_id` identifies the initial acknowledgment coordinator turn. It is
never a final-job result. `wait_for_task` requires `delegated_job_lifecycle`, reads the **origin** task and returns its
full terminal output and optional final `result_turn_id`. `delivery_error` reports
notification failure separately. A 404 or timeout means unknown outcome; it does
not establish nonexecution and does not trigger another delegation.

Use `BackgroundClientToolRunner` or `AsyncBackgroundClientToolRunner` with a
caller-selected durable `ToolStateStore`, explicit identity scope, and execution
chat ID. Feed the entire `client.chats.events(target_chat)` stream, including
replay and first ping. These runners apply replayed result/terminal barriers before
admitting pending requests, validate and unwrap `task.event.event_payload`, verify
its original signature, and deduplicate by call ID. They survive the initial turn
terminal and accept later synthesis and detached tools. Task cancellation stops
that task's handlers and prevents later admission; turn cancellation stops direct
turn handlers without cancelling detached children. Handler cancellation remains
cooperative for synchronous functions. Close the runner and stream explicitly
when the origin job terminates. Regular `ClientToolRunner`, `send_and_stream`, and
`send_and_wait` retain their foreground terminal behavior.

[examples/background_job.py](examples/background_job.py) provides a complete async
orchestration function that subscribes the target before delegation, services its
tools through synthesis, waits for final origin-task output, and closes both paths.
Tool outputs always go to the execution chat; cross-team tools are not forwarded
on the origin stream. Origin and existing target chats must share owner/workspace;
the target chat binding determines the team. Do not multiplex different identities
into a runner or deduplication scope. Recovery requires resident replay through
first ping plus the same durable call store; a missing replay/history or unknown
task requires application reconciliation, not resubmission.

Other primitives are `chats.deliver(..., body=NativeDeliveryRequest(...))`,
`chats.get_task(chat_id, task_id)`,
`chats.cancel_task(chat_id, task_id, body={"subtree": True})`, and
`chats.classify_phase(..., body=PhaseClassificationRequest(...))`. Cancellation is
best effort, cannot undo external effects, and can suppress pending delivery of an
already terminal task. Delivery accepts completed/blocked/failed/cancelled outcomes;
its receipt status is queued/delivered/cancelled/failed. Phase classification returns
exactly `BLOCKED` or `NOT BLOCKED`. Save its response: `request_id` admission is
durable but responses are not retained, and a 502/503/transport failure may have
incurred work. No mutation or classifier call is automatically retried.

Phase jobs should set `notify_origin=False` to suppress automatic model wake while
retaining task terminal/status output. `strict_model=True` on message/delegation
requests (also supported by foreground helpers) rejects unavailable configured
root models before admission and persists for synthesis; child model selection is
unchanged. `agent.done.requested_model` and optional `model` expose requested and
provider-reported IDs. Resident delivery receipts, task state and deduplication can
be lost after server restart/eviction. This source integration does not assert
compatibility of any previously published package.

### Managed delegation and recovery

Both clients provide `chats.delegate_and_wait(...)` and `chats.resume_delegation(...)`.
Pass a typed `TeamDelegationRequest`, registry, explicit durable store, and account/deployment
scope. These helpers open the target SSE before admission, inspect the origin snapshot
before releasing buffered tool requests, keep the stream alive through post-turn activity,
and return `DetachedTaskSnapshot` with the final `output` and `result_turn_id`. A completed,
failed, or cancelled snapshot is returned as-is; HTTP, verification and tool-output failures
raise. An exact `event_id` retry can resume a running resident job; `resume_delegation`
accepts the saved `TeamDelegationReceipt` and performs no new delegation POST.

```python
final = client.chats.delegate_and_wait(
    origin_chat, body=request, registry=registry, store=store,
    scope="deployment/account", timeout=300, poll_interval=0.5,
)
# Resume a receipt obtained with the low-level delegate method:
final = client.chats.resume_delegation(
    origin_chat, receipt, registry=registry, store=store, scope="deployment/account",
)
```

The stream owns the entire execution chat, so use one coordinated tool runner per chat.
Resident history may be unavailable after eviction; a task 404 is an unknown outcome and
never causes automatic resubmission. Caller cancellation is local and does not send remote
cancellation. Explicit `chats.cancel_task` remains a separate application decision.

Foreground idempotent helpers inspect all buffered result/terminal barriers before yielding
requests to consumers. Tool owner bindings and terminal barriers persist through the supplied
store, under the reserved `envai:internal:` scope namespace. User scopes cannot use that prefix.
A replay that changes a call's direct/task ownership raises `ToolStateError` without executing it.

### Durable distributed tool claims

`PostgresToolState` is available with `pip install 'envai-teams-sdk[postgres]'` alongside
`SQLiteToolState`. Provision `envai.postgres_tool_state.SCHEMA_SQL` explicitly using your
migration process, then construct `PostgresToolState(conninfo)`. Construction never creates
or repairs schema. PostgreSQL commits a unique claim before returning admission; independent
workers and restarted processes share deduplication, resolved barriers, ownership and task
terminal metadata. Keep each deployment/account scope stable across credential rotation.
Ambiguous crash claims never expire or execute automatically. Both stores contain identifiers
and hashes, not arguments, outputs or API credentials.

### Polling and request bounds

`chats.wait_for_task`, `wiki.wait_source`, `wiki.wait_operation`, image `generate_and_wait`,
and managed delegation share an overall budget including negotiation and HTTP requests.
`RequestOptions(deadline=time.monotonic() + seconds, cancel=threading_event)` also bounds
safe GET retries; `timeout` remains the HTTP request timeout. Poll sleeps respond to local
cancellation. Sync network reads use the remaining HTTPX timeout and recheck the deadline
after returning; Python cannot forcibly interrupt a custom blocking transport or a handler
that ignores cooperative cancellation. Async callers may also cancel the awaiting task.
Wiki waiters only read state and never initiate indexing or paid processing.

`completions.list(..., page=2)` provides the canonical page selector. `completions.iterate`
uses numeric pages with shared authentication, retry, error and cancellation behavior; it
never follows URLs supplied by the server. Use `request_options` for one traversal budget.
Client `headers={"X-Application": "my-service"}` adds application headers across authenticated
requests/SSE; authentication and negotiation headers cannot be overridden.

Diagnostic image requests accept `files={"files": [("a.png", image_bytes, "image/png"), ...]}`
and `data={"prompt": "...", "model_code": "..."}` in `test_generate_image`. The SDK encodes
one `files[]` multipart field per image and validates the canonical one-to-four bound.

### Portable definitions and deployment CLI

The `envai.definitions` package compiles JSON/YAML manifests and Markdown agents,
plans drift, and reconciles tools, agents and teams with durable state. It provides
`Definitions`, `ClientRemote`, `FileStore`, `PostgresStore`, explicit override options,
and `envai-definitions` commands. Validation runs offline; plans do not mutate the
remote. Ambiguous writes use read-back reconciliation and are never retried blindly.
See the [definitions guide](src/envai/definitions/README.md) and
[offline manifest example](examples/definitions.py). Deployment APIs are synchronous;
run them in a deployment/startup worker for async applications.

### Independent jobs and finite choices

The native surface is pinned to backend commit `ba021aa61e30e379880f809d45f90e5d3564d43e`. Native extensions remain opt-in and default to disabled. Capability discovery is read-only; admission requires `independent_jobs`, choice classification requires `finite_choice_classification`, and delivery without an origin requires `originless_deliveries`. Explicit `strict_model: true` also requires `strict_model`.

Independent jobs start without a parent turn. Their receipt identifies the initial coordinator; final task `output` and `result_turn_id` identify the completed job. Task snapshots keep the required `origin_turn_id` key with the empty string for independent jobs. Missing origin is invalid. Keep the execution chat’s native SSE subscription and client-tool runner alive while waiting; polling helpers do not execute tools.

Finite-choice requests provide a full non-null JSON context, question, stable request ID and 2–32 exact labels with descriptions. The SDK rejects duplicate or invalid labels and verifies the returned label, request ID, model and invocation ID. An invented label is a protocol error. Classification admission is durable and a duplicate returns 409; uncertain outcomes are never automatically resubmitted. Job and delivery deduplication is only resident: after restart, 404 means unknown, not permission to repeat paid work.

```python
client = EnvaiClient(api_key, native_tasks=True)
receipt = client.chats.start_job(chat_id, body={
    "workspace_id": workspace_id, "request_id": request_id,
    "prompt": "Review the supplied changes", "strict_model": True,
})
result = client.chats.wait_for_independent_job(chat_id, receipt.task_id)
selected = client.chats.classify_choice(chat_id, body={
    "request_id": classification_id, "context": {"output": result.output},
    "question": "Can this phase continue?",
    "choices": [{"label": "CONTINUE", "description": "No clarification required"},
                {"label": "ASK", "description": "Owner input required"}],
})
```

`AsyncEnvaiClient` exposes the same methods with `await`. Delivery accepts omitted or empty `origin_turn_id` and optional `strict_model`; existing nonempty-origin delivery remains unchanged.

## API key administration

API key list/create/delete methods remain discoverable but are unsupported by this bearer/project-key client. They raise `envai.errors.UnsupportedOperationError` (code `session_required`) before any HTTP request, including capability discovery. The server requires a human browser session and CSRF protection for `/api/v1/api-keys`; manage keys through that authenticated application flow. The SDK does not implement session authentication or bypass these checks.

## Project tenancy migration

Workspaces, chats, agents and teams now expose immutable `project_id` ownership and nullable `created_by_user_id` audit provenance instead of `user_id`. Completions expose `project_id`, `api_key_id` and nullable `user_id`; key activity is never attributed to a key creator. Migrate owner checks to project fields.

Tool registrations are project-wide, not user-wide. Use `create_project_tool`, `list_project_tools`, etc., or `client.tools`. Deprecated UserTool names forward to the same project-scoped operations and do not preserve user-wide visibility. A project key determines the project; an optional project header must match the authenticated project.

The event snapshot defines 39 typed ATP event types. The exact server HTTP snapshot contains 146 operations. This SDK generates only the reviewed 92-operation allowlist in `scripts/sdk-operations.json`: 89 callable operations plus 3 API-key administration stubs that fail locally. Human-session, internal, financial and billing administration methods are not generated. This is not full parity with all 146 server operations.

## Project resources, crons, skills and reports

Unreleased on this branch; 14 operations were added to the `1.0.0a6` baseline. Sync and async clients expose the same names (await the async ones).

```python
from envai import EnvaiClient, models, errors

with EnvaiClient(api_key, base_url=base_url) as client:
    client.chats.update(workspace_id, chat_id, body=models.ChatUpdateRequest(title="Q3 rollout plan"))  # title only
    skill = client.project_skills.create(
        project_id, body=models.ProjectSkillCreateRequest(name="incident_response", content="# Response\n"))
    crons = client.chat_crons.list(workspace_id, status="active")
    client.chat_crons.update(workspace_id, crons[0].id, body={"prompt": "Check the rollout"})
    cleanup = client.chats.delete(workspace_id, chat_id)  # HTTP 200, cleanup[0].status == "pending"
    client.project_skills.delete(project_id, skill.id)    # HTTP 204, returns None

    window = {"from": "2026-10-01", "to": "2026-10-31", "per_page": 50}
    for day in client.project_reports.iterate_usage(project_id, window):
        print(day.date, [(m.currency, m.amount) for m in day.totals])  # amounts are decimal strings
    analytics = client.project_reports.analytics(project_id, {"from": "2026-10-01", "to": "2026-10-07"})
    event_types = client.project_reports.filters(project_id).types
```

- New groups: `chat_crons` (list/get/update/delete), `project_skills` (list/create/get/update/delete) and `project_reports` (usage, logs, analytics, filters). `workspaces.delete`, `chats.update` and `chats.delete` extend existing groups. Crons have no create operation.
- Deleting a workspace or chat returns a `CleanupResult` with `pending` cleanup intents. This is a request accepted, not data purged. Cron and skill deletes return `None`. A 409 is surfaced as `ConflictError`; the SDK never stops crons or cancels work to retry a deletion.
- `project_reports` takes one filters dictionary with canonical wire keys (`from`, `to`, `api_key_id`, `chat_id`, `type`, `types[]`, `workspace_ids[]`, `api_key_ids[]`, `view`, `page`, `per_page`). Invalid filters (bad dates, windows over 90 days, unknown keys, conflicting scalar and array filters, view-prohibited keys) raise `ValueError` before any request.
- `iterate_usage` and `iterate_logs` page numerically and locally, preserve all filters including `per_page`, honor an explicit start page over `filters["page"]`, and never follow server URLs. `usage` and `logs` return pages with `meta`; `analytics` and `filters` return unwrapped data.
- The low-level `get_project_logs` returns the wrapped union of the three view payloads. Use the typed helpers when you want the unwrapped analytics or filter data.
- Log entries' `agent`/`data` of `[]` are coerced to `{}`; non-empty arrays are rejected.
- HTTP 402 raises `errors.PaymentRequiredError` (an `EnvaiError`). 503 `provider_configuration_error` is a `ServerError`; 503 `provider_error` remains `ProviderError` and `file_*` types remain `FileError`. Mutations are never retried and the SDK never funds an account after a 402.
- Project keys do not gain API-key administration or financial rights. Those operations remain unsupported.
- Four server effects are already typed and are not new: `chat.title.updated`, `chat.cron.wakeup`, `billing.limit_reached`, `billing.credit_exhausted`. They are nonterminal; billing notices may have a null `turn_id`. The ATP2 catalog is frozen, and native extensions remain opt-in.

See the [API reference](docs/api-reference.md#project-resources) for signatures and the [inventory](docs/api-inventory.md) for paths.
