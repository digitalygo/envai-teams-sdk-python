---
status: completed
created_at: 2026-10-20
files_edited:
  - docs/chat-attachments.md
  - docs/wiki.md
  - scripts/generate.py
  - src/envai/_contracts/openapi.json
  - src/envai/_contracts/openapi.yaml
  - src/envai/_contracts/operations.json
  - src/envai/_contracts/wiki.json
  - src/envai/_transport.py
  - src/envai/attachments.py
  - src/envai/models.py
  - src/envai/wiki.py
  - tests/fixtures/contract3-provenance.json
  - tests/fixtures/capabilities/canonical-v3.json
  - tests/fixtures/capabilities/provenance.json
  - tests/test_chat_attachments.py
  - tests/test_contract3_provenance.py
  - tests/test_http.py
  - tests/test_original_files.py
rationale:
  - Align original-file behavior with the merged server and PHP SDK.
  - Fail before uploading when original delivery is unavailable.
  - Preserve default wiki conversion and existing public feature types.
supporting_docs:
  - https://github.com/digitalygo/envai-teams/commit/7192fc05ebec817bddc705186c8f195cc0dc5ec5
  - https://github.com/digitalygo/envai-teams-sdk-php/commit/93eafc02271035065ac975f91cd4f77d148c9d2c
---

# Original chat files SDK alignment

## Summary of changes

Aligned synchronous and asynchronous upload, capability discovery, native readiness and optional original-only wiki staging with the merged server and PHP reference. Refreshed current contract snapshots and provenance while preserving frozen v2 artifacts.

## Technical reasoning

Native attachment uploads require both `chat_attachments` and `chat_attachment_originals` before multipart transmission. Existing attachment operations and attachment messages retain their previous capability requirement. A homogeneous string list or strict boolean feature map is accepted; maps normalize to enabled names without changing `Capabilities.features: list[str]`.

Attachment polling recognizes `not_required` only with lifecycle `ready`. Wiki `upload_file(..., processing="original")` adds a separate multipart field and requires `wiki_original_sources`, including low-level calls. Omitted or explicit `convert` preserves the helper's file-only upload. Original bytes and idempotency keys remain unchanged; mutations are never automatically retried or downgraded to OCR.

## Impact assessment

Baseline: `d78b3f8e14534a1f44a92a11472bcd04bf3a81ec`. Current schemas are pinned byte-for-byte to server `7192fc05ebec817bddc705186c8f195cc0dc5ec5`; PHP `93eafc02271035065ac975f91cd4f77d148c9d2c` supplies the implementation reference. The sibling Go and TypeScript SDKs were aligned in the same task. Server and PHP files were not modified.

No dependency, package version, workflow, migration, tag, release or deployment was changed. Work remains local and uncommitted. The request was resolved through existing upload, transport, generation and polling structures, without a new dependency or abstraction layer.

## Validation steps

The leader independently ran these checks with uv-managed Python 3.13.5 and a dedicated database on disposable PostgreSQL 18:

```bash
.venv/bin/python scripts/generate.py --check
.venv/bin/ruff check src scripts tests examples
.venv/bin/mypy src
ENVAI_REQUIRE_POSTGRES=1 pytest --cov=envai --cov-report=term-missing
.venv/bin/python -m build
.venv/bin/python -m unittest discover -s .github/release-tools/test -p '*_test.py'
npm test --prefix .github/release-tools
npm audit --audit-level=high --prefix .github/release-tools
git diff --check
```

The test command used the project virtual environment and `TEST_DATABASE_URL` pointing only to the disposable SDK test database. Results: 942 tests passed, zero skips, 95.81% aggregate branch-aware coverage. Changed handwritten executable lines were covered 49/49; changed generator statements were covered 2/2. Both native readiness lifecycle paths were explicitly tested. Generated schema/model behavior was checked through hydration, generation drift and immutable provenance tests. The built wheel imported outside the source tree with 71 operations. Five release Python tests and 91 release-tool tests passed; the dependency audit reported zero vulnerabilities.

Quality judgment: PASS. Focused cross-SDK security judgment: PASS. Reviewed 18-file source/test/documentation package SHA-256: `465622bc88587c021912f02505f3065d895ff87a0cb129f78fa6d6bfb838987e`, excluding this operation record. Non-blocking review suggestions were exact original-mode multipart assertions and direct model-map validation tests; current transport paths and capability denial are covered.

Python 3.11 and 3.12 remain CI-matrix checks, not locally claimed results. No production API, real user file or paid provider was used. The disposable PostgreSQL container was stopped and removed after combined verification.
