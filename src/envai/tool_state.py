"""Explicit at-most-once claims. No adapter is selected or file created implicitly."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Protocol

from .errors import ToolStateError

_STATUSES = frozenset({"running", "ambiguous", "submitted", "resolved"})


@dataclass(frozen=True)
class ToolRecord:
    fingerprint: str
    status: str
    turn_id: str


def _strings(*values) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ToolStateError("Tool claim identifiers and fingerprints must be nonempty strings")


def _status(status) -> None:
    if not isinstance(status, str) or status not in _STATUSES:
        raise ToolStateError("Invalid tool claim status")


def _record(value) -> ToolRecord:
    if not isinstance(value, ToolRecord):
        raise ToolStateError("Corrupt tool claim record")
    _status(value.status)
    _strings(value.turn_id)
    if not isinstance(value.fingerprint, str) or (
        not value.fingerprint and value.status != "resolved"
    ):
        raise ToolStateError("Corrupt tool claim fingerprint")
    return value


class ToolStateStore(Protocol):
    """Implement with durable atomic transactions for crash/restart safety.

    claim must commit BEFORE returning True. Existing claims must never expire
    automatically. transition may not overwrite resolved status. Scope identifies
    deployment AND account, never a secret. Do not share a scope across accounts.
    Existing malformed records must raise without being replaced or repaired.
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

    def _get(self, key):
        return _record(self._records[key]) if key in self._records else None

    def claim(self, scope, chat_id, call_id, fingerprint, turn_id):
        _strings(scope, chat_id, call_id, fingerprint, turn_id)
        with self._lock:
            key = (scope, chat_id, call_id)
            old = self._get(key)
            if old is not None:
                if (old.fingerprint and old.fingerprint != fingerprint) or old.turn_id != turn_id:
                    raise ToolStateError("Conflicting replay for an existing tool claim")
                return False
            self._records[key] = ToolRecord(fingerprint, "running", turn_id)
            return True

    def transition(self, scope, chat_id, call_id, status):
        _strings(scope, chat_id, call_id)
        _status(status)
        with self._lock:
            key = (scope, chat_id, call_id)
            old = self._get(key)
            if old is not None and old.status != "resolved":
                self._records[key] = ToolRecord(old.fingerprint, status, old.turn_id)

    def resolve(self, scope, chat_id, call_id, turn_id):
        _strings(scope, chat_id, call_id, turn_id)
        with self._lock:
            key = (scope, chat_id, call_id)
            old = self._get(key)
            if old is not None and old.turn_id != turn_id:
                raise ToolStateError("Conflicting turn for an existing tool claim")
            self._records[key] = ToolRecord(
                old.fingerprint if old is not None else "", "resolved", turn_id
            )

    def get(self, scope, chat_id, call_id):
        _strings(scope, chat_id, call_id)
        with self._lock:
            return self._get((scope, chat_id, call_id))


class SQLiteToolState:
    """Caller-selected local durable store; no args, outputs, or credentials stored.

    SQLite atomic transactions prevent concurrent duplicate execution. Preserve
    the database across reconnects/restarts. A crash after claim is deliberately
    ambiguous and must be reconciled by the application, never auto-reexecuted.
    Corrupt existing rows fail closed; the SDK never automatically repairs them.
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

    def _get(self, scope, chat_id, call_id):
        rows = self._db.execute(
            "SELECT fingerprint,status,turn_id FROM tool_claims WHERE scope=? AND chat_id=? AND call_id=?",
            (scope, chat_id, call_id),
        ).fetchmany(2)
        if len(rows) > 1:
            raise ToolStateError("Corrupt duplicate tool claim records")
        return _record(ToolRecord(*rows[0])) if rows else None

    def claim(self, scope, chat_id, call_id, fingerprint, turn_id):
        _strings(scope, chat_id, call_id, fingerprint, turn_id)
        with self._lock, self._db:
            # Acquire the database write lock before inspecting the existing row,
            # including when separate store instances share this database.
            self._db.execute("BEGIN IMMEDIATE")
            old = self._get(scope, chat_id, call_id)
            if old is not None:
                if (old.fingerprint and old.fingerprint != fingerprint) or old.turn_id != turn_id:
                    raise ToolStateError("Conflicting replay for an existing tool claim")
                return False
            self._db.execute(
                "INSERT INTO tool_claims VALUES (?,?,?,?,?,?)",
                (scope, chat_id, call_id, fingerprint, "running", turn_id),
            )
            return True

    def transition(self, scope, chat_id, call_id, status):
        _strings(scope, chat_id, call_id)
        _status(status)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            old = self._get(scope, chat_id, call_id)
            if old is not None and old.status != "resolved":
                self._db.execute(
                    "UPDATE tool_claims SET status=? WHERE scope=? AND chat_id=? AND call_id=?",
                    (status, scope, chat_id, call_id),
                )

    def get(self, scope, chat_id, call_id):
        _strings(scope, chat_id, call_id)
        with self._lock:
            return self._get(scope, chat_id, call_id)

    def resolve(self, scope, chat_id, call_id, turn_id):
        _strings(scope, chat_id, call_id, turn_id)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            old = self._get(scope, chat_id, call_id)
            if old is not None:
                if old.turn_id != turn_id:
                    raise ToolStateError("Conflicting turn for an existing tool claim")
                self._db.execute(
                    "UPDATE tool_claims SET status='resolved' WHERE scope=? AND chat_id=? AND call_id=?",
                    (scope, chat_id, call_id),
                )
            else:
                self._db.execute(
                    "INSERT INTO tool_claims VALUES (?,?,?,?,?,?)",
                    (scope, chat_id, call_id, "", "resolved", turn_id),
                )

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
