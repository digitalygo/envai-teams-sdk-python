"""Definition deployment CLI. Validation is entirely local and credential-free."""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Callable

from .compiler import compile_manifest
from .service import ClientRemote, Definitions, DriftError, SyncOptions
from .stores import FileStore, MemoryStore, MySQLStore, PostgresStore, SQLiteStore


def main(argv: list[str] | None = None, *, client_factory: Callable[..., Any] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="envai-definitions")
    parser.add_argument("command", choices=("validate", "plan", "sync", "status", "state"))
    parser.add_argument("--path", default=os.getenv("ENVAI_DEFINITIONS_PATH", "definitions"))
    parser.add_argument("--connection", default=os.getenv("ENVAI_CONNECTION", "default"))
    parser.add_argument("--store", choices=("file", "postgres", "sqlite", "mysql", "memory"),
                        default=os.getenv("ENVAI_STATE_DRIVER", "file"))
    parser.add_argument("--state-path", default=os.getenv("ENVAI_STATE_PATH", ".envai/state.json"))
    parser.add_argument("--only", choices=("all", "tools", "agents", "teams"), default="all")
    parser.add_argument("--json", action="store_true")
    for flag in ("adopt", "overwrite-drift", "recreate-missing", "prune", "fail-on-drift", "dry-run"):
        parser.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        if args.command == "validate":
            manifest = compile_manifest(args.path)
            result: Any = {"format_version": "1", "status": "valid", "manifest_hash": manifest.hash}
        else:
            store = (FileStore(args.state_path) if args.store == "file" else
                     PostgresStore(args.state_path) if args.store == "postgres" else
                     SQLiteStore(args.state_path) if args.store == "sqlite" else
                     MySQLStore(args.state_path) if args.store == "mysql" else MemoryStore())
            if args.command == "state":
                with store.lock(args.connection):
                    result = {"format_version": "1", "resources": store.resources(args.connection),
                              "ready": store.ready(args.connection)}
            else:
                if client_factory is None:
                    from envai import EnvaiClient
                    client_factory = EnvaiClient
                client = client_factory(api_key=os.environ["ENVAI_API_KEY"],
                                        base_url=os.getenv("ENVAI_BASE_URL", "http://localhost:8111/api/v1"))
                service = Definitions(ClientRemote(client), store, args.connection)
                options = SyncOptions(adopt=args.adopt, overwrite_drift=args.overwrite_drift,
                                      recreate_missing=args.recreate_missing, prune=args.prune,
                                      fail_on_drift=args.fail_on_drift, dry_run=args.dry_run,
                                      scope=args.only)
                if args.command == "sync":
                    result = service.sync(args.path, options)
                else:
                    result = service.plan(args.path, options)
                    result["status"] = "blocked" if any(op["blocking"] for op in result["operations"]) \
                        else "planned"
                    if args.command == "status":
                        result["ready"] = service.ready(args.path)
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
        return 2 if result.get("status") == "blocked" else \
            1 if result.get("status") in ("failed", "ambiguous") else 0
    except DriftError as exc:
        print(json.dumps({"format_version": "1", "status": "drift", "plan": exc.plan}))
        return 2
    except Exception as exc:
        print(json.dumps({"format_version": "1", "status": "error", "error_type": type(exc).__name__}))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
