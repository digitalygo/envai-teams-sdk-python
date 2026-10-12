"""Explicit at-most-once claims. No adapter is selected or file created implicitly."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Protocol

from .errors import ToolStateError


@dataclass(frozen=True)
class ToolRecord:
    fingerprint: str
    status: str
    turn_id: str


class ToolStateStore(Protocol):
    """Implement with durable atomic transactions for crash/restart safety.

    claim must commit BEFORE returning True. Existing claims must never expire
    automatically. transition may not overwrite resolved status. Scope identifies
    deployment AND account, never a secret. Do not share a scope across accounts.
    """

    def claim(
        self, scope: str, chat_id: str, call_id: str, fingerprint: str, turn_id: str
    ) -> bool: ...
    def transition(self, scope: str, chat_id: str, call_id: str, status: str) -> None: ...
    def get(self, scope: str, chat_id: str, call_id: str) -> ToolRecord | None: ...
    def resolve(self, scope: str, chat_id: str, call_id: str, turn_id: str) -> None: ...


class MemoryToolState:
    """Explicitly ephemeral; replay protection lasts only for this object lifetime."""

    def __init__(self):
        self._records: dict[tuple[str, str, str], ToolRecord] = {}
        self._lock = threading.Lock()

    def claim(self, scope, chat_id, call_id, fingerprint, turn_id):
        with self._lock:
            key = (scope, chat_id, call_id)
            old = self._records.get(key)
            if old:
                if (old.fingerprint and old.fingerprint != fingerprint) or old.turn_id != turn_id:
                    raise ToolStateError("Conflicting replay for an existing tool claim")
                return False
            self._records[key] = ToolRecord(fingerprint, "running", turn_id)
            return True

    def transition(self, scope, chat_id, call_id, status):
        with self._lock:
            key = (scope, chat_id, call_id)
            old = self._records.get(key)
            if old and old.status != "resolved":
                self._records[key] = ToolRecord(old.fingerprint, status, old.turn_id)

    def resolve(self, scope, chat_id, call_id, turn_id):
        with self._lock:
            key = (scope, chat_id, call_id)
            old = self._records.get(key)
            self._records[key] = ToolRecord(old.fingerprint if old else "", "resolved", turn_id)

    def get(self, scope, chat_id, call_id):
        with self._lock:
            return self._records.get((scope, chat_id, call_id))


class SQLiteToolState:
    """Caller-selected local durable store; no args, outputs, or credentials stored.

    SQLite atomic insertion prevents concurrent duplicate execution. Preserve the
    database across reconnects/restarts. A crash after claim is deliberately
    ambiguous and must be reconciled by the application, never auto-reexecuted.
    """

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS tool_claims (
            scope TEXT, chat_id TEXT, call_id TEXT, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL, turn_id TEXT NOT NULL,
            PRIMARY KEY(scope,chat_id,call_id))""")
        self._db.commit()

    def claim(self, scope, chat_id, call_id, fingerprint, turn_id):
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO tool_claims VALUES (?,?,?,?,?,?)",
                (scope, chat_id, call_id, fingerprint, "running", turn_id),
            )
            if cursor.rowcount == 1:
                return True
            old = self._db.execute(
                "SELECT fingerprint,turn_id FROM tool_claims WHERE scope=? AND chat_id=? AND call_id=?",
                (scope, chat_id, call_id),
            ).fetchone()
            if not old or (old[0] and old[0] != fingerprint) or old[1] != turn_id:
                raise ToolStateError("Conflicting replay for an existing tool claim")
            return False

    def transition(self, scope, chat_id, call_id, status):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE tool_claims SET status=? WHERE scope=? AND chat_id=? AND call_id=? AND status!='resolved'",
                (status, scope, chat_id, call_id),
            )

    def get(self, scope, chat_id, call_id):
        with self._lock:
            row = self._db.execute(
                "SELECT fingerprint,status,turn_id FROM tool_claims WHERE scope=? AND chat_id=? AND call_id=?",
                (scope, chat_id, call_id),
            ).fetchone()
            return ToolRecord(*row) if row else None

    def resolve(self, scope, chat_id, call_id, turn_id):
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO tool_claims VALUES (?,?,?,?,?,?) ON CONFLICT(scope,chat_id,call_id) DO UPDATE SET status='resolved'",
                (scope, chat_id, call_id, "", "resolved", turn_id),
            )

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
