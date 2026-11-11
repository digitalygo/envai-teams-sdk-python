"""Optional PostgreSQL tool claims. Provision SCHEMA_SQL explicitly before use."""
from __future__ import annotations

import threading

from .errors import ToolStateError
from .tool_state import ToolRecord, _record, _status, _strings

SCHEMA_SQL = """CREATE TABLE envai_tool_claims (
    scope TEXT NOT NULL, chat_id TEXT NOT NULL, call_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, status TEXT NOT NULL, turn_id TEXT NOT NULL,
    PRIMARY KEY (scope, chat_id, call_id)
)"""


class PostgresToolState:
    """Distributed durable claims; no automatic DDL, expiry, replay, or repair.

    Install the postgres extra and provision SCHEMA_SQL using caller-owned migration
    credentials. Each instance owns its connection; independent workers may share
    the table. Existing ambiguous claims require application reconciliation.
    """

    def __init__(self, conninfo: str):
        try:
            import psycopg
        except ImportError as exc:
            raise ImportError("Install envai-teams-sdk[postgres] for PostgresToolState") from exc
        self._db = psycopg.connect(conninfo, autocommit=True)
        self._lock = threading.Lock()

    def _get(self, scope, chat_id, call_id, *, lock=False):
        row = self._db.execute(
            "SELECT fingerprint,status,turn_id FROM envai_tool_claims "
            "WHERE scope=%s AND chat_id=%s AND call_id=%s" + (" FOR UPDATE" if lock else ""),
            (scope, chat_id, call_id),
        ).fetchone()
        return _record(ToolRecord(*row)) if row else None

    def _insert(self, scope, chat_id, call_id, fingerprint, status, turn_id):
        return self._db.execute(
            "INSERT INTO envai_tool_claims VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING call_id",
            (scope, chat_id, call_id, fingerprint, status, turn_id),
        ).fetchone() is not None

    def claim(self, scope, chat_id, call_id, fingerprint, turn_id):
        _strings(scope, chat_id, call_id, fingerprint, turn_id)
        with self._lock, self._db.transaction():
            if self._insert(scope, chat_id, call_id, fingerprint, "running", turn_id):
                return True
            old = self._get(scope, chat_id, call_id, lock=True)
            if old is None:
                raise ToolStateError("Claim disappeared during transaction")
            if (old.fingerprint and old.fingerprint != fingerprint) or old.turn_id != turn_id:
                raise ToolStateError("Conflicting replay for an existing tool claim")
            return False

    def get(self, scope, chat_id, call_id):
        _strings(scope, chat_id, call_id)
        with self._lock:
            return self._get(scope, chat_id, call_id)

    def transition(self, scope, chat_id, call_id, status):
        _strings(scope, chat_id, call_id)
        _status(status)
        with self._lock, self._db.transaction():
            old = self._get(scope, chat_id, call_id, lock=True)
            if old is None:
                raise ToolStateError("Missing tool claim during status transition")
            if old.status != "resolved":
                self._db.execute(
                    "UPDATE envai_tool_claims SET status=%s WHERE scope=%s AND chat_id=%s AND call_id=%s",
                    (status, scope, chat_id, call_id),
                )

    def resolve(self, scope, chat_id, call_id, turn_id):
        _strings(scope, chat_id, call_id, turn_id)
        with self._lock, self._db.transaction():
            if self._insert(scope, chat_id, call_id, "", "resolved", turn_id):
                return
            old = self._get(scope, chat_id, call_id, lock=True)
            if old is None or old.turn_id != turn_id:
                raise ToolStateError("Conflicting turn for an existing tool claim")
            self._db.execute(
                "UPDATE envai_tool_claims SET status='resolved' WHERE scope=%s AND chat_id=%s AND call_id=%s",
                (scope, chat_id, call_id),
            )

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
