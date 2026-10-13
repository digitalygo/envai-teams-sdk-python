# Releases

CI runs the complete SDK checks before publishing a GitHub release. Pull requests receive read-only validation and never receive release or notification secrets. Pushes to `main`, `alpha`, `beta`, and `dev` are eligible when those branches exist. Branch creation does not change the default branch or branch protections.

## Conventional Commits

- `feat`: minor; `fix`: patch
- Breaking `feat` or `fix`, declared with `!` or a `BREAKING CHANGE` footer: major
- `chore`, `refactor`, `build`: patch
- `ci`, `docs`, `test`, `style`, `perf`: no release
- Commits containing `[skip ci]`: excluded from release analysis

Semantic Release chooses the highest applicable change since the previous release. Existing `v`-prefixed tags and releases are preserved. `main` produces stable `vX.Y.Z` tags; the other channels produce tags such as `v1.0.0-alpha.1`. An untagged repository starts with Semantic Release's initial version, `1.0.0` on its selected channel. This does not make an alpha release stable.

The manual **Trigger build** workflow creates exactly one empty `chore` commit on the selected releasing branch. The resulting push runs CI and then the ordinary release pipeline. A `chore` trigger is therefore expected to produce a release.

## Existing secrets

- `RELEASE_TOKEN` is required. A missing token fails the release job explicitly. The workflow never falls back to `GITHUB_TOKEN` because that would suppress downstream workflow events.
- `KCHAT_WEBHOOK_URL` is optional. When configured, the existing webhook receives the repository, tag, commit, run link, and release outcome. Missing configuration is reported as skipped without failing the release. Webhook URLs and token values must never be logged or committed.

Organization-level secrets must include these public repositories in their access policy. A reference in workflow YAML does not prove that a secret is available to a run. CI does not modify security or approval policies.

## Package distribution

GitHub release creation and registry publication are separate. The separately approved PyPI publisher uploads the verified release wheel and source distribution through the configured `pypi` trusted-publishing environment. It can also publish an existing release through manual dispatch. Other registries still require their own verified setup. These workflows do not create accounts or credentials.

- PHP: register `digitalygo/envai-teams-sdk-php` with Packagist, then configure its approved GitHub synchronization. Composer versions follow Git tags.
- TypeScript: the declared package is `@digitalygo/envai-client`. Verify ownership of the `digitalygo` npm scope and configure an approved npm publisher before enabling registry uploads. GitHub releases include an installable `.tgz` built with the computed release version.
- Python: the declared PyPI name is `envai-teams-sdk`. The approved trusted publisher is bound to `digitalygo/envai-teams-sdk-python`, workflow `publish-pypi.yml`, environment `pypi`. GitHub releases include wheel and source distributions; `1.0.0-alpha.1` is normalized to Python's `1.0.0a1`.
- Go: the public module path is `github.com/digitalygo/envai-teams-sdk-go`. Versioned Git tags are the publication mechanism; consumers can request a tag through the public Go module proxy.

TypeScript and Python release preparation commits only the computed package metadata using `chore(release): VERSION [skip ci]`, pushes without force, and then tags that commit. Tagged source archives and uploaded package assets therefore agree on their version. A rejected metadata push fails before creating the tag. Go releases at major version 2 or later require the matching `/vN` suffix in `go.mod` before tagging.

Registry uploads must remain disabled until the intended namespace, owner, authentication and target have been verified. A successful GitHub release alone does not demonstrate a successful npm, PyPI, or Packagist publication.

## Maintenance

Release tooling is isolated under `.github/release-tools` with a committed npm lockfile and lifecycle scripts disabled during installation. CI tests the release policy and audits the dependency graph. Renovate is configured for each repository's actual package managers, with Wednesday 11:00–13:00 Europe/Rome updates and CODEOWNERS reviewers. The configuration requires a Renovate service with access; adding its JSON file alone does not install that service.

These SDK repositories have no production Docker image, so container publication and Swarm deployment are intentionally absent.
