# Wiki V1 / contract3 validation

## Chat attachments contract update (2026-10-16)

Current source schemas are pinned to backend commit `96acf00162984af4a86a671f924c53a18960fae6`, with 71 HTTP operations. The attachment guide documents asynchronous conversion, whole-message rejection, explicit retry, retention and independent conversion costs. Source manifests identify exact upstream bytes separately from generated inventories and synthetic fixtures. Historical HTTP captures retain their original source commits. This update does not deploy the backend.

Local checks: 838 tests passed with required PostgreSQL and 95.75% coverage. Ruff, Mypy, deterministic generation, sdist/wheel builds and isolated wheel import passed.

## Earlier validation records

The following counts, SDK heads and backend pins describe earlier checkpoints. They do not attest the current attachment implementation.

Validated against Python SDK alpha baseline `b36a0539d11a337e585a674c54ee04cae88bb6b4` (tree `bf6150d0ddd9a4397765d8cb3b8f8f3a9d3dfe09`) and backend commit [`c27963d0848540679bccf30d2aeab354097b1a38`](https://github.com/digitalygo/envai-teams/commit/c27963d0848540679bccf30d2aeab354097b1a38). All five bundled shared schemas match independently retrieved remote Git blob IDs and local SHA256 byte hashes. Historical original fixture provenance remains unchanged and separate.

## Final checks

- Python3.11.17 and3.13.5: **805 tests passed on each runtime, no skips**, with real PostgreSQL18.3 and SQLite. Resource warnings and unraisable exceptions were treated as errors.
- Python3.12.14: full real-PostgreSQL suite **797 passed**, followed by all **8 newly added agent/tool cost-bearing event cases passed**. Together these cover the final805-case suite. No paid provider or production service was used.
- Changed executable coverage: **845/869, 97.24%**, including generated DTOs/methods and generator changes. Full runtime coverage including generated code: **95.48%** (line and branch metric). Per-file delta details are in `wiki-delta-coverage.json`; documentation/schema data are validated separately.
- Ruff, mypy, deterministic generator drift, JSON Schema/fixture/provenance checks, actionlint and Git whitespace checks: passed.
- Wheel and source distribution build, archive allowlist/metadata validation, and wheel import/resources outside the source checkout: passed. All71 operations and v2/v3/Wiki schemas are packaged.
- Release-tool suite: **91 passed**, matcher coverage100%; package archive unit suite: **5 passed**. Locked npm audit: no known vulnerabilities.
- The proposed `feat(wiki)!` commit with a `BREAKING CHANGE` trailer is classified as a major change by the repository's actual Semantic Release analyzer. Normal alpha release automation owns package versions/tags and publishing.

CI now starts an official `postgres:18` service independently for every Python3.11/3.12/3.13 matrix job. `TEST_DATABASE_URL` is a disposable synthetic CI database, `psycopg[binary]` is a development dependency, and `ENVAI_REQUIRE_POSTGRES=1` makes a missing database fail rather than silently skip durability tests.

## Covered behavior

- Default3 selector, canonical versioned ATP namespace, acknowledged protocol/capability preflight before mutations, no silent fallback, no ambiguous mutation resubmission, and future capability features without weakening version checks.
- Explicit legacy read-only mode across generated/friendly methods; complete original numeric v2 validation, nonfinite/negative/nested cost rejection, missing required ATP fields, nullable3 and numeric2 across all five cost-bearing event families.
- Atomic finite legacy replay: no prefix is exposed for wrong-chat, duplicate/backward/before-cursor sequence, gap, malformed/oversized/partial or mixed/incompatible tails. V3 retains live duplicate suppression, exact historical payloads and selected reconnect routes/headers.
- Sync/async multipart file streaming and size limits; source status/download, diagnostics, lifecycle staging, runs, catalog revisions, exhaustive GET/truncation protection, and existing artifact APIs without fabricated writer/QUERY endpoints.
- Exact raw-byte Ed25519 vector, signing-header duplication/malformation, canonical base64url, strict key metadata, trusted-origin JWKS caching/rotation, replay bounds, workspace binding, event discriminator/state validation, and separate provider-HMAC protocol.
- Durable receipt plus local application outbox in one database transaction, payload collisions, connection scoping, six independent competing processes, real process exits before/after commit, rollback on second-write failure, and async cancellation racing commit. A committed duplicate does not create another job. Remote downstream effects remain explicitly at-least-once/idempotency-owned.
- Decimal original currencies and pending/empty costs, immutable terminal metadata versus revised current costs, turn binding, and exact synthetic authenticated control-plane source/pending/legacy completion response fixtures.

## Confirmed fixes included

The generator previously discarded root properties when an object also had `allOf` and emitted its model text before discovering inline request models. Both are fixed with regressions preserving required usage fields and generated Wiki request DTOs. The existing SQLite corruption-test fixtures used a transaction context without closing their raw connections; explicit closure and closed-handle assertions fix the Python3.13 resource warnings without removing or weakening corruption checks.

## Reproduce

```sh
python -m pip install -e '.[dev]'
python scripts/generate.py --check
ruff check src scripts tests examples
mypy src
# Point only at a disposable local database; the tests create/drop SDK test tables.
export TEST_DATABASE_URL='postgresql://test_user:test_password@127.0.0.1:5432/test_database'
export ENVAI_REQUIRE_POSTGRES=1
pytest --cov=envai --cov-report=term-missing
python -m build
python -m unittest discover -s .github/release-tools/test -p '*_test.py'
actionlint
git diff --check
```

The inclusive coverage run removes the baseline exclusions for `_generated.py`, `models.py` and `event_models.py` in a separate coverage configuration. The delta intersects executable coverage lines with added/modified lines from the verified baseline; generated declarations and wrappers are included. Generator coverage is measured separately for both generation and `--check` and included in the delta.

These checks do not establish production provider quotas, backend/FFS deployment or remote CI for the future SDK publication commit. Backend/FFS review and deployment remain separate. No SDK branch, tag, release or PR was created by this validation.
