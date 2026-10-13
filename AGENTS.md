# Quality checks

Run the checks covering every changed behavior before finishing. Fix failures and rerun the affected checks. Never weaken or delete tests to make a run green.

Run the complete CI commands listed in `.github/workflows/ci.yml` for the affected SDK and run `npm test --prefix .github/release-tools` for release automation changes. Validate workflow YAML with actionlint and run `git diff --check`.

Use Conventional Commits. `feat` and `fix` describe consumer-visible changes; `chore`, `refactor`, and `build` also release patches. `ci`, `docs`, `test`, `style`, and `perf` do not trigger releases. Do not alter existing tags, release history, repository permissions, or security policies.

Keep code cohesive and names descriptive. Preserve all existing test coverage and report checks that could not run.
