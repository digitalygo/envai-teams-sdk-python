# Python API reference

[README](../README.md) · [Reviewed 92-operation subset](api-inventory.md) · [Streaming](streaming.md) · [Client tools](client-tools.md)

## Services and primitives

`EnvaiClient` and `AsyncEnvaiClient` expose the same operation names. Await async methods. Service groups are `workspaces`, `tools`, `chats`, `chat_crons`, `project_skills`, `project_reports`, `completions`, `files`, `agents`, `teams`, `models`, `api_keys`, `images`, and `capabilities`. The three project groups are new in this unreleased source; the `1.0.0a6` baseline does not have them.

Every reviewed contract operation (the 92-operation subset supported by this SDK; not the full 146 server operations) also has a snake_case primitive directly on the client: `sendChatMessage` becomes `send_chat_message`. Inspect [generated signatures](../src/envai/_generated.py) for exact positional/keyword arguments and return annotations. [The inventory](api-inventory.md) lists HTTP paths and operation IDs; paths are relative to `/api/v1` except origin-level JWKS.

Use generated request types from `envai.models`, or supported JSON dictionaries where a method accepts them. API field names stay snake_case. A missing optional field is omitted from JSON; explicit null is allowed only for nullable fields. Existing `ApiKey` model definitions (such as `ApiKeyCreate.expires_at` or `ApiKeyCreatedData.token`) document the historical or session-bound schema; API key creation is unsupported in this bearer client and blocked locally by stubs. Never log plaintext keys or tokens.

Resource and ordinary collection `data` wrappers are unwrapped into typed results. Completion pages, usage pages and project log pages retain `data`, `links`, and `meta`. IDs are strings. Legacy numeric usage values preserve the wire representation. Project usage amounts (`UsageMoney.amount`) are exact decimal strings such as `"0.100000000000"`, never floats; nullable fields stay `None`. The SDK does not derive billing from local price tables.

## Pagination and files

`completions.iterate(workspace_id, chat_id)` yields individual completions across numeric pages using the standard client transport. The bundled OpenAPI schema defines canonical `page` and `per_page` query parameters on `listCompletions`. The iterator increments `page` locally, yields from `result.data`, and stops when `result.links.next` is falsy or `result.meta.last_page` is reached. It never follows response-supplied URLs, raises `ProtocolError` if pagination metadata is inconsistent, and each page request uses the normal GET retry wrapper.

Multipart inputs use HTTPX-style file tuples. Opening local files is an application decision. Binary download methods return `bytes`; they do not write files automatically, and large downloads require enough memory for the returned content.

`images.download_signed_url(url)` validates the configured origin and artifact endpoint, preserves signed query bytes/order, and omits bearer credentials. The primitive accepts opaque `artifact`, `generation`, `signature`, and optional `expires` fields. Do not replace these with local paths or log signed URLs. Legacy `path` artifact URLs are unsupported.

`images.generate_and_wait` polls the generation until a terminal status or timeout; inspect the final status. A polling timeout does not prove a remote generation was cancelled. Provider-webhook and image-test primitives are exposed for contract completeness, not automatically called in normal workflows.

## Project resources

New in this unreleased source (backend contract `ba021aa61e30e379880f809d45f90e5d3564d43e`). Every method below exists on both `EnvaiClient` and `AsyncEnvaiClient` with identical names; async methods are awaited. Bodies accept the typed model or a dictionary. Omitted optional body fields are not sent; explicit `None` is sent only for nullable fields.

| Group | Methods | Result |
| --- | --- | --- |
| `workspaces` | `delete(workspace_id)` | `models.CleanupResult` |
| `chats` | `update(workspace_id, chat_id, body=...)`, `delete(workspace_id, chat_id)` | `models.Chat`, `models.CleanupResult` |
| `chat_crons` | `list(workspace_id, *, origin_chat_id=None, status=None)`, `get(workspace_id, cron_id)`, `update(workspace_id, cron_id, body=...)`, `delete(workspace_id, cron_id)` | `list[models.ChatCron]`, `models.ChatCron`, `models.ChatCron`, `None` |
| `project_skills` | `list(project_id)`, `create(project_id, body=...)`, `get(project_id, skill_id)`, `update(project_id, skill_id, body=...)`, `delete(project_id, skill_id)` | `list[models.ProjectSkill]`, `models.ProjectSkill` (x3), `None` |

All of them also accept `request_options=` and, for cron methods, an optional keyword `x_envai_project_id`. The primitives use the snake_case names listed in the [inventory](api-inventory.md#method-names-for-the-new-operations).

```python
from envai import EnvaiClient, models

with EnvaiClient(api_key, base_url=base_url) as client:
    chat = client.chats.update(workspace_id, chat_id, body=models.ChatUpdateRequest(title="Q3 rollout plan"))
    skill = client.project_skills.create(
        project_id,
        body=models.ProjectSkillCreateRequest(name="incident_response", content="# Response\n"),
    )
    client.project_skills.update(project_id, skill.id, body={"description": None})
    crons = client.chat_crons.list(workspace_id, status="active")
    client.chat_crons.update(workspace_id, crons[0].id, body={"prompt": "Check the rollout"})
```

Behavior to rely on:

- `chats.update` accepts only `title` (a string or `None`). Any other key fails locally with `envai.errors.ValidationError` before a request is sent.
- Skill `content` is Markdown and is preserved byte for byte, including leading whitespace. An empty update body `{}` is sent as `{}`.
- Crons have no create operation in this contract. `update` is a `PATCH`.
- `chat_crons.delete` and `project_skills.delete` return `None` (HTTP 204).
- `workspaces.delete` and `chats.delete` return HTTP 200 with a `CleanupResult` whose `cleanup` entries start as `pending` (`pending`, `processing`, `completed`, `failed`). The response means cleanup was requested, not that data has been purged. The SDK does not poll or confirm purging.
- A 409 on deletion (for example an active cron) is surfaced as `ConflictError`. The SDK never stops crons or cancels work to make a deletion succeed.
- A project key determines the project. These methods give a project key no API-key administration or financial rights.

## Project reports

`client.project_reports` is a handwritten helper over `getProjectUsage` and `getProjectLogs`. Filters are one dictionary using the canonical wire names: `from`, `to`, `api_key_id`, `chat_id`, `type`, `view`, `page`, `per_page`, and the bracket keys `types[]`, `workspace_ids[]`, `api_key_ids[]`. Unknown keys, `None` values and invalid values raise `ValueError` before any request. The helpers send no `X-Envai-Project-Id` header.

| Method | Allowed filter keys | Returns |
| --- | --- | --- |
| `usage(project_id, filters=None)` | `from`, `to`, `api_key_id`, `page`, `per_page` | `models.UsageReportCollection` |
| `usage_page(project_id, page=None, filters=None)` | same | `models.UsageReportCollection` |
| `iterate_usage(project_id, filters=None, start_page=None)` | same | iterator of `models.UsageReportDay` |
| `logs(project_id, filters=None)` | `from`, `to`, `api_key_id`, `chat_id`, `type`, `types[]`, `workspace_ids[]`, `api_key_ids[]`, `page`, `per_page` | `models.ProjectLogCollection` |
| `logs_page(...)`, `iterate_logs(...)` | same as `logs` | `models.ProjectLogCollection`, iterator of `models.ProjectLogEntry` |
| `analytics(project_id, filters=None)` | `from`, `to`, `api_key_id`, `chat_id` | `models.ProjectActivityAnalytics` (unwrapped) |
| `filters(project_id, filters=None)` / `event_types(...)` | `from`, `to` | `models.ProjectEventTypeFilters` (unwrapped) |

```python
page = client.project_reports.usage(project_id, {"from": "2026-10-01", "to": "2026-10-31", "per_page": 50})
for day in client.project_reports.iterate_usage(project_id, {"from": "2026-10-01", "to": "2026-10-31"}):
    print(day.date, [(total.currency, total.amount) for total in day.totals])

events = client.project_reports.logs(project_id, {"types[]": ["turn.completed", "turn.failed"], "per_page": 100})
analytics = client.project_reports.analytics(project_id, {"from": "2026-10-01", "to": "2026-10-07"})
known_types = client.project_reports.filters(project_id).types
```

Validation, all local:

- `from` and `to` are `YYYY-MM-DD` real dates; `from <= to` and the inclusive window is at most 90 days.
- `api_key_id` and `chat_id` must match the `key_` and `cht_` ULID formats. `type` is at most 80 characters.
- Bracket lists hold 1 to 50 unique strings; item length limits are 80 (`types[]`), 30 (`workspace_ids[]`, `api_key_ids[]`). A list is sent as repeated keys (`types[]=a&types[]=b`).
- `type` conflicts with `types[]`, and `api_key_id` conflicts with `api_key_ids[]`.
- `per_page` is 1 to 100; `page` is a positive integer.
- `analytics` and `filters` reject event-only filters (`type`, `types[]`, `workspace_ids[]`, `api_key_ids[]`, `page`, `per_page`), and `filters` also rejects `api_key_id` and `chat_id`. A conflicting `view` key is rejected.
- The returned payload must match the requested view, otherwise `ProtocolError`.

Pagination is local and numeric. `iterate_usage` and `iterate_logs` start at the explicit `start_page`, else at `filters["page"]`, else at page 1, keep all other filters on every request, stop at `meta.last_page`, and never follow a response-supplied URL. An explicit `page` argument to `usage_page` or `logs_page` overrides `filters["page"]`. A response whose `meta.current_page` differs from the requested page, or whose `last_page` is lower, raises `ProtocolError`. Page 1 is omitted from the query unless `filters` contained `page`.

The low-level `get_project_usage` and `get_project_logs` do not unwrap `data`. `get_project_logs` returns the union of the three wrapper types; pass `view="analytics"` or `view="filters"` yourself and read `.data`. Its keyword names are `from_`, `types`, `workspace_ids`, `api_key_ids`; they map to the wire keys `from`, `types[]`, `workspace_ids[]`, `api_key_ids[]`. The same restrictions are applied locally (for example `view="analytics"` with `page` fails before any request). The helpers `analytics` and `filters` unwrap `data`; `logs` and `usage` keep `meta` and `links`.

Log entries: a legacy server value of `[]` for an entry's `agent` or `data` is coerced to `{}`. Only the empty list is coerced; a non-empty array or non-object entry is rejected as `ProtocolError`. `api_key_id`, `actor_api_key_id` and `turn_id` may be `None`. Unknown additive response fields are preserved.

## Events and errors

Typed event classes live in `envai.event_models`. Known events (39 ATP event types in the pinned snapshot) are checked against their exact ATP2 numeric or ATP3 nullable payload schemas; malformed known events raise `ProtocolError`. A valid additive type becomes `UnknownEvent` with original content preserved. Capabilities discovery is not a parser allowlist.

Four server effects are already typed and are not new in this source: `chat.title.updated`, `chat.cron.wakeup`, `billing.limit_reached` and `billing.credit_exhausted`. None is terminal and none contributes response text. Billing notices may carry `turn_id=None` when emitted before admission or a real `turn_id` when emitted mid-turn. Company credit notices (`billing.credit_exhausted`) carry no amounts (only scope, currency, and reason); limit notices (`billing.limit_reached`) carry optional decimal string `amount` and `remaining` values. The frozen ATP2 catalog is unchanged; only `chat.title.updated` is valid under `contract_version=2`. Native extensions stay opt-in.

`EnvaiError` and subclasses expose `http_status`, `type`, `code`, `message`, `details`, and `request_id`. Branch on code/status. HTTP 402 raises `PaymentRequiredError` (an `EnvaiError`; existing `except EnvaiError` handlers still catch it, for example `code="credit_exhausted"`). Status 503 with type `provider_configuration_error` raises `ServerError`; 503 with type `provider_error` still raises `ProviderError`; error types starting with `file_`, and `invalid_path`, still raise `FileError`. The exception string excludes potentially sensitive server details; explicit properties and `ProtocolError.raw` may still contain private content.

[Transport configuration](getting-started.md#client-configuration) and [stream recovery](streaming.md) describe timeout and retry behavior. Mutation methods are sent once, including on 402 and 503: the SDK never retries a mutation, never funds an account after a 402, and never stops crons after a 409. Generated models and operation coverage do not imply every server feature is currently available; preserve 404/409/validation responses rather than treating them as success.

## Wiki and contract selection

See [Wiki V1](wiki.md) for typed upload-first sources, conversion diagnostics, runs, catalog GETs, exact-byte webhooks and durable application outbox. See [contract3 migration](migration-contract3.md) before upgrading. Both sync and async expose the reviewed 92-operation subset (89 callable operations plus 3 unsupported API-key stubs), including explicit legacy read helpers.

## API key administration

API key list/create/delete methods remain discoverable but are unsupported by this bearer/project-key client. They raise `envai.errors.UnsupportedOperationError` (code `session_required`) before any HTTP request, including capability discovery. The server requires a human browser session and CSRF protection for `/api/v1/api-keys`; manage keys through that authenticated application flow. The SDK does not implement session authentication or bypass these checks.

## Project tenancy migration

Workspaces, chats, agents and teams now expose immutable `project_id` ownership and nullable `created_by_user_id` audit provenance instead of `user_id`. Completions expose `project_id`, `api_key_id` and nullable `user_id`; key activity is never attributed to a key creator. Migrate owner checks to project fields.

Tool registrations are project-wide, not user-wide. Use `create_project_tool`, `list_project_tools`, etc., or `client.tools`. Deprecated UserTool names forward to the same project-scoped operations and do not preserve user-wide visibility. A project key determines the project; an optional project header must match the authenticated project.

The exact server snapshot (backend commit `ba021aa61e30e379880f809d45f90e5d3564d43e`) contains 146 operations. This SDK generates only the reviewed 92-operation allowlist in `scripts/sdk-operations.json`: 89 callable operations plus 3 API-key administration stubs that fail locally. Human-session, internal, financial and billing administration methods are not generated. This is not full parity with all 146 server operations.
