# Signed client tools

[README](../README.md) · [Configuration](getting-started.md) · [Streaming](streaming.md)

## Registration and lifecycle

Three separate choices are required: register an application handler in `ClientToolRegistry`, create the intended account tool with `client.tools.create`, and enable its name in the selected agent's `external_tools_enabled`. The SDK does not synchronize these automatically. Repeating a remote create is not an upsert.

The complete [async echo example](../examples/client_tools.py) uses `SQLiteToolState` and binds its runner to the accepted chat/turn. Set `ENVAI_BASE_URL`, `ENVAI_API_KEY`, `ENVAI_CHAT_ID`, `ENVAI_TOOL_STATE_PATH`, and a stable `ENVAI_ACCOUNT_SCOPE`, then run `python examples/client_tools.py`. Configure `echo` remotely and enable it on the chat's agent first. This sends a real, potentially billable turn.

Choose `ClientToolRunner` for synchronous handlers or `AsyncClientToolRunner` for native async handlers. Pass explicit `store`, `scope`, `chat_id`, and `turn_id`; the runner does not execute unrelated turns. Feed all matching request, result, and terminal events through `consume`. Close the runner and call `wait` to surface delivery errors. For async runners use `await aclose()` and `await wait()`.

## Verification and schema compatibility

The signed payload is `call_id + "\n" + tool_name + "\n" + RFC8785(args)`, verified with Ed25519 using a trusted public JWKS endpoint. It does not sign the chat, turn, account, timestamp, or freshness. Do not execute arbitrary archived requests as fresh work. JWKS never receives bearer authentication; key refresh and cache behavior are configurable through the verifier.

Arguments are snapshotted and normalized to binary64 before verification and handler execution. Use strings for exact large integers outside ±(2^53−1). Non-finite values are rejected. Registered schemas require an object root and cannot fetch external references; literal `const`/`enum` objects stay data.

An omitted dialect uses JSON Schema 2020-12. Explicit Draft 3, 4, 6, 7, 2019-09, and 2020-12 use the corresponding installed `jsonschema` validator; unsupported dialects fail registration. Prefer Draft 6 or newer for tools shared with TypeScript. The exact schema constraints matter more than the Python argument annotations.

## Application authorization and isolation

Handlers execute as ordinary application code with that process's privileges. The SDK does not create a sandbox or install filesystem/shell handlers. Authenticate the intended account and enforce resource authorization before every consequential effect. Signed arguments still require an application policy.

If implementing file/command tools, restrict allowed paths and operations, defend against symlink/path traversal, use explicit argument arrays instead of shell interpolation, cap output, and apply OS isolation and least privilege. Check `context.check_cancelled()` before effects and propagate timeouts to downstream services. Never return secrets in tool output.

## Durable state and ambiguous effects

`SQLiteToolState` uses a caller-selected file with atomic insertion and FULL synchronous commits. Preserve it and the stable non-secret scope across process restarts and key rotation. A scope must separate accounts and deployments. `MemoryToolState` is ephemeral and cannot protect effects across restarts. Distributed workers need a shared transactional implementation of `ToolStateStore`; do not assume a local SQLite file coordinates different hosts.

The key is `(scope, chat_id, call_id)`. Store records retain hashes/status, not arguments, outputs, or credentials. States are `running`, `ambiguous`, `submitted`, and `resolved`. A claim commits before execution; conflicting payloads are rejected. The runner records `ambiguous` before output delivery, `submitted` after HTTP success, and `resolved` on a matching stream result. Results observed before requests create durable tombstones.

Corrupt records and missing claims during transitions fail closed. Never automatically expire, steal, delete, repair, or re-execute claims. A crash after an external effect but before durable completion is inherently ambiguous. Use `context.call_id` as an idempotency key at the downstream system, use a transaction, or reconcile manually. Deduplication does not guarantee exactly-once effects.

## Bounds, cancellation, and output

Concurrency defaults to four handlers with no unbounded queue. Saturation produces a controlled error. Handler timeouts reserve a conservative submission margin; the server timer began before receipt, so local timing cannot prove the remote time remaining. Result/terminal observations cancel matching local work.

Sync handlers run in worker threads. Python cannot forcibly stop an arbitrary thread; a non-cooperative timed-out handler keeps its slot until it exits. Async handlers must cooperate with task cancellation. Cancellation cannot undo an already committed external effect.

Strings are sent unchanged; structured results are JSON-serialized once. Output must be nonempty UTF-8 and no more than 128 KiB. Unknown tools, invalid arguments, handler failures, and invalid output produce sanitized errors. The SDK never blindly resubmits output after an ambiguous response. A subsequent 404/409 does not establish earlier delivery; use matching result events and authoritative application state for reconciliation.
