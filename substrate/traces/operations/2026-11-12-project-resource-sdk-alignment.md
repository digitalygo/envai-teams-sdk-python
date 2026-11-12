---
status: completed
created_at: 2026-11-12
files_edited:
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
supporting_docs:
  - ../../../AGENTS.md
  - ../../../docs/api-reference.md
  - ../../../docs/api-inventory.md
  - ../../../tests/fixtures/contract3-provenance.json
  - https://github.com/digitalygo/1-1-5-envai-aaas/tree/ba021aa61e30e379880f809d45f90e5d3564d43e
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
