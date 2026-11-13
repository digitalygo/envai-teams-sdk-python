---
status: completed
created_at: 2026-11-12
updated_at: 2026-11-14
files_edited:
  - docs/model-selection.md
  - src/envai/_contracts/events.json
  - tests/test_turn_model.py
  - .github/workflows/ci.yml
  - README.md
  - docs/api-inventory.md
  - docs/api-reference.md
  - docs/getting-started.md
  - docs/migration-contract3.md
  - scripts/generate.py
  - scripts/sdk-operations.json
  - src/envai/_contracts/openapi.json
  - src/envai/_contracts/openapi.yaml
  - src/envai/_contracts/operations.json
  - src/envai/_generated.py
  - src/envai/_transport.py
  - src/envai/client.py
  - src/envai/errors.py
  - src/envai/models.py
  - src/envai/project_reports.py
  - tests/fixtures/contract3-provenance.json
  - tests/test_contract3_provenance.py
  - tests/test_contracts.py
  - tests/test_events.py
  - tests/test_http.py
  - tests/test_project_reports.py
  - tests/test_project_resources.py
  - tests/test_project_tenancy.py
  - tests/test_wiki_contract3.py
rationale:
  - Align synchronous and asynchronous project-key clients with the existing backend resources.
  - Preserve strict wire validation, error compatibility and safe local report pagination.
  - Carry an exact model override through synchronous and asynchronous turn helpers.
  - Pin the reviewed model-only server commit and reject boundary Unicode White_Space in model values, with no adoption of unrelated server API changes.
supporting_docs:
  - https://github.com/digitalygo/envai-teams/tree/7f4085dbfdd3f208b82716119cff1860fbd502bb
  - ../../../docs/model-selection.md
  - ../../../AGENTS.md
  - ../../../docs/api-reference.md
  - ../../../docs/api-inventory.md
  - ../../../tests/fixtures/contract3-provenance.json
  - https://github.com/digitalygo/1-1-5-envai-aaas/tree/ba021aa61e30e379880f809d45f90e5d3564d43e
  - https://github.com/digitalygo/envai-teams/commit/f970714c0a358ff94b14f15eabde197c55861e74
---

# Project-resource SDK alignment

## Summary of changes

Aligned both Python clients with backend `feature/user-company-projects`, pinned
at `ba021aa61e30e379880f809d45f90e5d3564d43e`. Work is on
`feature/user-company-projects`, based on clean, synchronized `alpha` at
`e742b3a8da47968858ae7ef63ea12d83a8d66436`.

Added 14 existing project-key operations, taking the allowlist to 92 operations:
89 callable operations and three locally rejected API-key management stubs.
The full backend snapshot contains 146 operations.

## Technical reasoning

The existing generator, HTTPX transport and Pydantic models already provided the
necessary foundation. The allowlist and generated groups were extended for
project skills, manual crons, project usage/logs, chat title updates and
workspace/chat deletion. Both synchronous and asynchronous clients expose the
same additions.

- Added typed helpers for usage pages, log event pages, analytics and the event
  catalog. Numeric pagination preserves filters and explicit start pages and
  never follows server-provided next URLs.
- Repeated bracketed query keys preserve array semantics. View restrictions,
  scalar/plural conflicts and list constraints fail locally rather than silently
  changing the request.
- Normalization accepts only exact empty log-entry `agent` and `data` arrays as
  objects before strict validation and hydration. Requested and returned report
  views must agree; unrelated malformed payloads still fail.
- Pydantic omission remains distinct from explicit null. Markdown whitespace,
  decimal money strings, nullable attribution and additive fields are retained.
- Workspace/chat deletion exposes the HTTP 200 cleanup state; cron/skill
  deletion returns HTTP 204. No automatic cancellation or retry circumvents 409.
- HTTP 402 adds `PaymentRequiredError` under the existing `EnvaiError` base.
  Independent verification corrected an overbroad 503 mapping so ordinary
  provider and file errors retain their previous subclasses, while
  `provider_configuration_error` maps to `ServerError` with its code preserved.
- Existing title, cron and billing events remain typed and nonterminal. Billing
  notices may have a null turn before admission. Native extensions remain
  opt-in and frozen ATP v2 is unchanged.
- Refreshed OpenAPI and derived artifacts, with six byte-exact source pins to
  the canonical backend repository. Historical capture provenance is unchanged.

CI changes only the installed-wheel inventory assertion from 78 to 92.
Repository ownership and public visibility were verified before that edit, and
the shared CI reference was read. Runner selection, triggers, permissions,
secrets and release behavior are unchanged.

## Impact assessment

These are unreleased additions on top of the `1.0.0a6` source baseline. No
package version, dependency manifest, release policy or database schema was
changed. This operation does not commit, push, publish or deploy the SDK.
Backend and PHP sources were read only.

Human-session administration and financial management remain unavailable through
project-key clients. No public cron-create endpoint, paid-message replay or
automatic funding action was introduced. The historical unsupported-log
assertion was replaced with approved positive coverage and stronger human-only
operation exclusions; no tests were deleted or skipped to pass verification.

## Validation steps

The orchestrator independently ran:

- Generator drift check, Ruff and mypy: passed; 27 source files type checked.
- Full pytest suite on Python 3.13.5 with isolated MySQL 8.4 and PostgreSQL 18:
  1,619 passed, zero skips.
- Combined line/branch coverage: 95.95%, above the unchanged 90% gate.
  Independent changed-source line coverage: 240/240, 100%. Existing generated
  code exclusions remain unchanged; operation surface tests exercise generated
  methods separately.
- Source and wheel build: passed. The wheel was installed in a disposable venv
  and imported outside the source tree; 92 operations, client methods and the
  canonical schema hash were verified.
- CI helper checks: 5 Python tests, 91 Node release-tool tests, npm audit and
  actionlint passed. `git diff --check` and Markdown structural checks passed.
- All six source snapshots matched backend bytes, and frozen v2 matched the
  SDK baseline.
- Independent quality judgment: PASS. Independent security review: PASS.

The first full run, concurrent with CPU-heavy SDK checks, hit the existing
2-second deadlines in two fresh-delegation tests. A complete sequential rerun
passed without any intervening code change, relaxed deadline or skipped test.
This timing sensitivity remains an environment signal, not a hidden success.
Python 3.11 and 3.12 were not run locally. No live backend/provider test is
claimed. Review also noted the existing direct-primitive `None` omission versus
helper-filter `None` rejection convention.

Evidence is retained in `/tmp/envai-sdk-python-leader-rerun.log`,
`/tmp/envai-sdk-python-leader-junit.xml`,
`/tmp/envai-sdk-python-leader-coverage.xml`,
`/tmp/envai-sdk-python-wheel-leader.log`, and
`/tmp/envai-sdk-python-ci-tools-leader.log`. The reviewed non-trace snapshot is
`b891fdc624eb61eab3d450b1132865a7e018a075ca9345a229f41b5adb4ffec8`.

The backend advanced independently during verification without changes to the
pinned contracts, request/resource definitions or routes. Temporary test
databases were isolated by SDK; their containers and private connection files
are removed at task closure.

## Update: 2026-11-13 per-turn model selection

### Summary and reasoning

Starting from SDK `195c576` on `feature/user-company-projects`, refreshed the
current contracts to backend `7f4085dbfdd3f208b82716119cff1860fbd502bb`. The
backend pull already contained the `alpha` merge, and its remote PR 58 was
current. Verified GitHub metadata now names it `digitalygo/envai-teams`;
historical source and capture records above are not rewritten.

Added generated message `model` and catalog `selectable` fields. All four
synchronous and asynchronous stream/wait helpers forward a call-local model
into the message body rather than stream options. A missing-value sentinel
preserves omission versus explicit `None`, which request validation rejects.
The SDK neither changes model spelling nor prefetches a catalog, imposes an
allowlist, alters defaults, retries paid mutations or substitutes a model.
Existing strict-model gates and attachment idempotency remain unchanged.

### Impact and verification

No dependency, workflow, version, endpoint or backend source change was made
in this update. All six source contracts match the new backend pin, with v2
and historical captures preserved. The new guide documents input conventions,
error codes and the distinction between selected and observed models.

Independent generation checks, Ruff and mypy passed. The final full run passed
1,662 tests without skips on Python 3.13.5 with isolated SQL databases and
96.11% combined coverage. All five changed executable statements, including
the enclosing calls whose new keyword lines lack separate instrumentation,
were covered. Source and wheel builds passed; the wheel was installed into a
fresh environment and imported outside the source tree with 92 operations.

The initial concurrent run exposed an unchanged test's 5 ms deadline: it
observed one request rather than two. The isolated 31-test polling suite and
a complete sequential rerun passed without source changes or relaxed tests.
This remains a timing signal, not a claimed baseline reproduction.

Quality judgment and security review returned PASS for non-trace snapshot
`baaf2245901907cfc292cba74c5108292b6681b34453f3a4e9ed55e217a6c493`.
The quality reviewer noted that the helper annotation includes `object`, so
non-string model values are rejected at runtime rather than by static type
checking. Python 3.11/3.12 and actionlint were not run locally. No live backend,
provider, release or deployment verification is claimed.

Evidence remains under `/tmp/envai-model-sdk-verify/leader-python-*`. Task
containers, database volumes, private connection files and the wheel test
environment were removed after verification, leaving unrelated resources intact.

## Update: 2026-11-14 model input validation follow-up

### Follow-up summary of changes

A minor finding on the model input was corrected. The four SDK OpenAPI copies and the six source provenance and current-docs assertions were synced to the reviewed server commit [f970714c0a35](https://github.com/digitalygo/envai-teams/commit/f970714c0a358ff94b14f15eabde197c55861e74), which holds only the canonical `not.pattern` that forbids the 25 exact Go Unicode `White_Space` code points at the start or end of `model`. Python DTOs for `model` use a scoped `Field(strict=True)`, generated from the source generator, so `bytes` and `bytearray` are rejected without coercion. An explicit `None` in the constructor is preserved and rejected on send.

### Follow-up technical reasoning

The server runtime was already correct and is unchanged; the published schema is now as strict. The rule uses a YAML-decoded Unicode positive class, so it avoids Python final line feed and ECMAScript NEL escape differences. The string type, `minLength`, optional omission and non-null wire behavior are unchanged, and nothing is coerced, trimmed, normalized or case-folded. Valid Unicode IDs, interior whitespace and the `FEFF` and `001C` to `001F` code points that Go does not treat as space stay accepted. Null and omission semantics of other DTO types are not tightened globally.

The server working `alpha` branch has also merged unrelated Wiki PR 60 and image PR 61. The SDKs intentionally pin the reviewed model-only commit instead of adopting those unrelated API features. The pin commit exists locally and its push is pending; it will be a public `alpha` ancestor before any SDK push, and publication is not claimed yet. The user explicitly approved pushing server `alpha` and the four SDK `feature/user-company-projects` branches, with repo-admin team membership and bypass verified by the leader and no policy change.

### Follow-up impact assessment

No package version, dependency, CI, endpoint, count, authentication, billing, provider, hold or restart change was made, and no deployment, paid or live provider test, or new release is claimed. Public DTO behavior is stricter only for byte-like `model` values.

### Follow-up validation steps

- Python 3.13.5: generator `--check`, ruff and mypy passed. The full SQL pytest run passed 1926 tests with zero skips and 96.09% coverage; the build passed. A fresh `1.0.0a6` wheel was installed outside the source tree and verified for isolated import, strict DTO rejection and the corrected bundled schema.
- The leader instrumented the modified generator lines 117 to 118: 2/2 executed, with both `if` branch outcomes covered at 100%. Generated metadata has no independent line instrumentation, but 307 model tests verify the DTO and transport paths in both synchronous and asynchronous clients.
- Documentation-only record update on 2026-11-14. Checked that the five operation records are the only files written by this step and that the reviewed source files remain identical to the manifest at `/tmp/envai-sdk-model-minor-20261113/review/manifest.json` (50 changed paths across the five repositories; trace records are excluded from the gate).
- Quality judgment `PASS` and security review `PASS` over combined source and test snapshot `cb56581c5f2e5c9f947901e63a56521d126a3bdee4a025afc48339952e902e60` (lexically sorted repository name and path, NUL, bytes, NUL). All 50 paths were read and their diffs matched. Security review found no authentication, payer, provider or retry change and verified the six pinned sources.
- This is local completion of the code task. Commit and push are pending, so green remote CI and remote publication are not claimed. Python 3.11 and 3.12, Go 1.24 and 1.26, other PHP editions, Laravel 11 and 12, and Node 22 were not run locally; the remote CI matrices run after commit and push. `git diff --check` passed in every repository.
- Test resources were limited to task-named isolated Podman PostgreSQL and MySQL containers; `envai-netbird-demo-*` resources were not touched. Cleanup stays pending until the publication tests complete, and the leader owns removal at closure.
