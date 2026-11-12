# Installation and configuration

[README](../README.md) · [API reference](api-reference.md) · [Streaming](streaming.md) · [Client tools](client-tools.md)

## Package installation

The package version in this source tree is the `1.0.0a6` baseline (git tag `v1.0.0-alpha.6`), which already includes [contract3/Wiki V1 support](migration-contract3.md). The source on this branch is **unreleased**: it adds project resources (chat title update, workspace and chat deletion, chat crons, project skills, project usage and log reports), 92 operations and the new `PaymentRequiredError`. Tag `v1.0.0-alpha.6` is an unreleased repository baseline; publication to PyPI is not claimed unless verified on the package index. Installing an existing package does not provide the new features until a release is cut from this branch. To evaluate them, install from a reviewed source commit. This guide asserts no newly published version.

Requires Python 3.11 or newer. Use an isolated virtual environment and a supported, patched Python release.

```sh
python -m venv .venv
# POSIX shells:
. .venv/bin/activate
python -m pip install "envai-teams-sdk==1.0.0a6"
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. `1.0.0a6` is an alpha prerelease baseline; confirm it is available from your package index if installing prebuilt wheels. APIs may change. The exact version pin opts into this prerelease without a `--pre` flag. Review upgrades deliberately and keep your application's dependency lock file.

The distribution is `envai-teams-sdk`; application imports use `envai`. The corresponding GitHub release tag is [`v1.0.0-alpha.6`](https://github.com/digitalygo/envai-teams-sdk-python/releases/tag/v1.0.0-alpha.6). Its Python package version follows PEP 440 as `1.0.0a6`. The package contains the contracts and type marker; runtime validation does not retrieve remote schemas.

## Source installation and examples

For the examples and source checkout matching the release:

```sh
git clone --branch v1.0.0-alpha.6 https://github.com/digitalygo/envai-teams-sdk-python.git
cd envai-teams-sdk-python
python -m pip install .
```

Run these commands in your activated environment. For development, use `python -m pip install -e '.[dev]'` and the checks in the README. The tag checkout matches the baseline and does not contain this unreleased source. To work on unreleased changes, explicitly choose the moving `alpha` branch, the `feature/user-company-projects` branch, or a reviewed commit SHA instead of the release tag.

## Credentials and first request

Set `ENVAI_API_KEY`, `ENVAI_BASE_URL`, and an existing `ENVAI_TEAM_ID` in your application environment. Use a trusted HTTPS API URL in production. The SDK accepts HTTP for local development and does not enforce HTTPS automatically. Never put credentials in URLs, source control, logs, or example output.

From the checkout, run `python examples/quickstart.py`. This creates a workspace and chat and sends a real, potentially billable message. It prints the terminal type and raises if the turn failed or was cancelled. It does not delete the created resources. Use returned IDs as opaque strings.

For an existing chat, set `ENVAI_CHAT_ID` and run `python examples/stream_turn.py`. For tools, follow the separate [registration and safety guide](client-tools.md) first.

## Project skills and usage reports

Unreleased source only. These calls read or change project data with your project key; they never administer API keys or funding.

```python
import os
from envai import EnvaiClient, errors

project_id = os.environ["ENVAI_PROJECT_ID"]  # an existing prj_ identifier
with EnvaiClient(os.environ["ENVAI_API_KEY"], base_url=os.environ["ENVAI_BASE_URL"]) as client:
    skills = client.project_skills.list(project_id)
    try:
        usage = client.project_reports.usage(project_id, {"from": "2026-10-01", "to": "2026-10-07"})
    except errors.PaymentRequiredError as error:  # HTTP 402, never retried
        print(error.code, error.request_id)
    else:
        for day in usage.data:
            print(day.date, [(total.currency, total.amount) for total in day.totals])
```

Amounts are exact decimal strings. Report filters are validated locally. See the [API reference](api-reference.md#project-resources) for every method, filter and pagination rule.

## Client configuration

Both `EnvaiClient` and `AsyncEnvaiClient` accept:

| Option | Default / behavior |
| --- | --- |
| `contract_version` | `3` by default; explicit `2` is read-only. Selection and acknowledgment are mandatory for negotiated APIs. |
| `api_key` | Required, explicit positional credential. No environment lookup inside the SDK. |
| `base_url` | Local development URL `http://localhost:8111/api/v1`; explicitly override in production. Missing `/api/v1` is appended to the supplied path. |
| `jwks_url` | Origin-level `/.well-known/jwks.json`; override only with a trusted signing-key URL. No bearer token is sent there. |
| `timeout` | 30 seconds for ordinary request I/O. |
| `connect_timeout` | 10 seconds. |
| `stream_read_timeout` | 45 seconds for idle reads. |
| `retry` | `RetryPolicy(max_retries=2, base_delay=0.25, max_delay=10.0)` for eligible GETs. |
| `transport` | An HTTPX sync/async transport, for custom routing, TLS, proxies, or mocks. |
| `observer` | Optional callback receiving a structural event name and details. |

HTTPX environment proxy discovery is disabled (`trust_env=False`); configure a transport explicitly if a proxy is required. Redirects are not followed. Keep TLS verification enabled in custom transports.

Reuse clients and close them with `with` / `async with`. Async clients must use async transports. Streams also need explicit closure, particularly when breaking before a terminal. Sync cancellation is cooperative; async task cancellation is supported. Neither automatically cancels remote work.

## Verification scope

The bundled contract is pinned to backend commit `ba021aa61e30e379880f809d45f90e5d3564d43e` (92 operations). Tests use HTTP mocks, signed fixture data, and temporary local stores. They do not require Envai credentials or contact a production API. Passing tests proves the client's bundled-contract behavior; verify your deployment URL, access, team configuration, and available capabilities separately.
