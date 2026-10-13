"""Durable receipt + application outbox. No remote effects occur in intake.

SQLite uses FULL synchronous file-backed transactions. PostgreSQL uses psycopg
and synchronous_commit=on. Run explicit create_schema during application setup.
A committed job is at-least-once; consumers need downstream idempotency.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .errors import WebhookStoreError
from .wiki_webhooks import AsyncWikiWebhookVerifier, Headers, WikiWebhookVerifier


class ReceiptOutcome(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    COLLISION = "collision"


@dataclass(frozen=True)
class WikiOutboxJob:
    id: str
    event_id: str
    workspace_id: str
    event_type: str
    raw_body: bytes = field(repr=False)


class _Store:
    dialect: str

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def _connect(self) -> Any:
        raise NotImplementedError

    def create_schema(self) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS envai_wiki_receipts (id TEXT PRIMARY KEY, event_id TEXT NOT NULL, workspace_id TEXT NOT NULL, payload_hash TEXT NOT NULL)"
            )
            binary = "BYTEA" if self.dialect == "postgres" else "BLOB"
            db.execute(
                f"CREATE TABLE IF NOT EXISTS envai_wiki_outbox (id TEXT PRIMARY KEY REFERENCES envai_wiki_receipts(id), event_id TEXT NOT NULL, workspace_id TEXT NOT NULL, event_type TEXT NOT NULL, raw_body {binary} NOT NULL, completed INTEGER NOT NULL DEFAULT 0)"
            )

    def _persist(self, body: bytes, event: Any, connection_scope: str) -> ReceiptOutcome:
        if not connection_scope or re.search(r"[\x00-\x1f\x7f]", connection_scope):
            raise ValueError("A stable connection scope is required")
        identity = hashlib.sha256(
            (connection_scope + "\0" + event.workspace_id + "\0" + event.event_id).encode()
        ).hexdigest()
        digest = hashlib.sha256(body).hexdigest()
        try:
            with closing(self._connect()) as db, db:
                db.execute(
                    "BEGIN IMMEDIATE"
                    if self.dialect == "sqlite"
                    else "SET LOCAL synchronous_commit = on"
                )
                cursor = db.execute(
                    self._sql(
                        "INSERT INTO envai_wiki_receipts (id, event_id, workspace_id, payload_hash) VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING"
                    ),
                    (identity, event.event_id, event.workspace_id, digest),
                )
                inserted = cursor.rowcount == 1
                row = db.execute(
                    self._sql(
                        "SELECT payload_hash FROM envai_wiki_receipts WHERE id = ?"
                        + (" FOR UPDATE" if self.dialect == "postgres" else "")
                    ),
                    (identity,),
                ).fetchone()
                if row is None:
                    raise WebhookStoreError("Receipt insert was not readable")
                if row[0] != digest:
                    return ReceiptOutcome.COLLISION
                if inserted:
                    db.execute(
                        self._sql(
                            "INSERT INTO envai_wiki_outbox (id, event_id, workspace_id, event_type, raw_body) VALUES (?, ?, ?, ?, ?)"
                        ),
                        (identity, event.event_id, event.workspace_id, event.event_type, body),
                    )
            return ReceiptOutcome.ACCEPTED if inserted else ReceiptOutcome.DUPLICATE
        except (ValueError, WebhookStoreError):
            raise
        except Exception as exc:
            raise WebhookStoreError("Durable Wiki intake failed; do not acknowledge") from exc

    def pending(self, *, limit: int = 100) -> list[WikiOutboxJob]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with closing(self._connect()) as db, db:
            rows = db.execute(
                self._sql(
                    "SELECT id, event_id, workspace_id, event_type, raw_body FROM envai_wiki_outbox WHERE completed = 0 ORDER BY id LIMIT ?"
                ),
                (limit,),
            ).fetchall()
            return [WikiOutboxJob(row[0], row[1], row[2], row[3], bytes(row[4])) for row in rows]

    def complete(self, job_id: str) -> None:
        """Acknowledge only after the downstream effect is durably idempotent."""
        with closing(self._connect()) as db, db:
            db.execute(
                self._sql("UPDATE envai_wiki_outbox SET completed = 1 WHERE id = ?"), (job_id,)
            )

    def receive(
        self, verifier: WikiWebhookVerifier, body: bytes, headers: Headers, *, connection_scope: str
    ) -> ReceiptOutcome:
        event = verifier.verify(body, headers)
        return self._persist(body, event, connection_scope)

    async def create_schema_async(self) -> None:
        await asyncio.to_thread(self.create_schema)

    async def receive_async(
        self,
        verifier: AsyncWikiWebhookVerifier,
        body: bytes,
        headers: Headers,
        *,
        connection_scope: str,
    ) -> ReceiptOutcome:
        event = await verifier.verify(body, headers)
        # Cancellation may race with commit. Retrying the same event is safe.
        return await asyncio.to_thread(self._persist, body, event, connection_scope)

    async def pending_async(self, *, limit: int = 100) -> list[WikiOutboxJob]:
        return await asyncio.to_thread(self.pending, limit=limit)

    async def complete_async(self, job_id: str) -> None:
        await asyncio.to_thread(self.complete, job_id)


class SqliteWikiWebhookInbox(_Store):
    dialect = "sqlite"

    def __init__(self, path: str | Path):
        if not str(path) or str(path) == ":memory:" or str(path).startswith("file:"):
            raise ValueError("Use a persistent SQLite file path")
        self.path = Path(path)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA foreign_keys = ON")
        return db


class PostgresWikiWebhookInbox(_Store):
    dialect = "postgres"

    def __init__(self, conninfo: str):
        self._conninfo = conninfo

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise WebhookStoreError(
                "Install envai-teams-sdk[postgres] for PostgreSQL intake"
            ) from exc
        return psycopg.connect(self._conninfo)
