# Portable definitions

`Manifest` accepts ordinary Python dictionaries; `compile_manifest(path)` reads a
JSON/YAML manifest or a directory with `tools/*.json|yaml`, `agents/*.md|json|yaml`,
and `teams/*.json|yaml`. Markdown agents require YAML frontmatter (`format: 1`,
`mode: primary|subagent`) followed by their system prompt. Client handler functions
stay in `ClientToolRegistry`; executable code never enters hashes, state or requests.

```python
from envai.definitions import ClientRemote, Definitions, FileStore, SyncOptions

service = Definitions(ClientRemote(client), FileStore(".envai/state.json"),
                      scope="account-stable-id/production")
plan = service.plan("definitions")
report = service.sync("definitions", SyncOptions(dry_run=True))
# Explicit deployment, after review:
report = service.sync("definitions")
assert report["status"] == "succeeded" and report["ready"]
```

Use a stable account/deployment scope across credential rotation. Plans distinguish
local edits, remote drift, conflicts, missing managed resources, and adoption.
`adopt`, `overwrite_drift`, `recreate_missing`, and `prune` are explicit opt-ins.
Conflicts are never overridden. Adoption alone records actual remote state without
changing it; differing desired state remains unready. Tools use immutable names;
agent/team updates retain stored IDs even after rename. Creates run tools, agents,
then teams, substituting actual agent IDs. Prunes reverse this dependency order and
only delete managed resources. Partial scopes and dry runs never mark the complete
manifest ready. Remote model catalog failures abort planning.

File state uses private atomic durable snapshots and an exclusive process lock.
`PostgresStore(dsn)` provides multi-host locking and transactional durable writes;
call `create_schema()` explicitly during your own provisioning step. Construction
never connects or issues DDL. Each successful operation is saved before the next;
a later failure does not pretend to roll back remote effects. `MemoryStore` is
explicitly ephemeral and cannot provide restart safety.

Before each mutation, a durable intent record marks its outcome ambiguous. A lost
response gets one authoritative read-back, never a second mutation. A later sync
can recover a proven landed desired payload (or proven deletion) without repeating
the mutation; unresolved intent blocks even with all override flags. Inspect state
and the authoritative server before operator repair; deleting unresolved state can
cause duplicate effects. Reports redact exception messages. Keep state private:
it contains definitions and their last-applied payloads, but no credentials.

`ready(source)` only compiles and reads local state. `require_synchronized(source)`
raises when that exact manifest is not ready. `tool(source, key)`,
`agent(source, key)`, and `team(source, key)` return immutable `ResourceIdentity`
values containing the stable remote ID and remote name, checking readiness and
identity under one state lock; these lookups never access the network. `ensure(source, mode="lazy")`
rechecks under the lock and synchronizes only when the hash/TTL is stale; failed
reconciliation raises. `deploy`, `manual`, and `off` modes never auto-apply. These
are synchronous deployment APIs; async applications should run deployment in their
startup/deployment worker rather than blocking an active event loop.

```
envai-definitions validate --path definitions --json
envai-definitions plan --path definitions --connection account/production --json
envai-definitions sync --path definitions --dry-run
envai-definitions status --path definitions
envai-definitions state --state-path .envai/state.json
```

Environment: `ENVAI_BASE_URL`, `ENVAI_API_KEY`, `ENVAI_CONNECTION`,
`ENVAI_DEFINITIONS_PATH`, `ENVAI_STATE_DRIVER` (`file`, `sqlite`, `postgres`, `mysql`, `memory`), and
`ENVAI_STATE_PATH` (file path or PostgreSQL DSN). Validation never needs credentials
or network access. Plan/status do not mutate the remote or persist resource state.
Use `--only tools|agents|teams`, `--adopt`, `--overwrite-drift`, `--recreate-missing`,
`--prune`, `--fail-on-drift`, `--dry-run` explicitly. Blocked/drift results exit 2;
ordinary errors or ambiguous outcomes exit 1. No command enables overrides itself.


`SQLiteStore(path)` and `MySQLStore(mysql_dsn_or_connection_factory)` provide the same
state interface. Provision each with an explicit `create_schema()` call before deployment;
constructors perform no I/O. SQLite uses a companion advisory lock file spanning remote
reconciliation and independently committed resource snapshots. MySQL 8 uses a session
advisory lock and transactional JSON snapshots. Its `VARBINARY` account scope stores exact
UTF-8 bytes: case, accents, and trailing spaces never share identity. Install the `mysql`
extra for DSN connections; a connection factory can supply your TLS/driver policy. Factories
return fresh connections owned and closed by the store. CLI supports `--store sqlite` or
`--store mysql` with the path/DSN in `--state-path`; credentials never enter state or reports.
