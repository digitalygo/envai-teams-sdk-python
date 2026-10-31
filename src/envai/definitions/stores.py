"""Durable deployment state, independent of signed-tool execution state."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

from .compiler import DefinitionError


class MemoryStore:
    """Explicitly ephemeral state; useful for tests, not process recovery."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"format_version": "1", "scopes": {}}
        self._lock = threading.RLock()

    @contextmanager
    def lock(self, scope: str) -> Iterator[None]:
        with self._lock:
            yield

    def _scope(self, scope: str) -> dict[str, Any]:
        return self.data["scopes"].setdefault(scope, {"resources": {}, "ready": None})

    def resources(self, scope: str) -> dict[str, Any]:
        return deepcopy(self._scope(scope)["resources"])

    def ready(self, scope: str) -> dict[str, Any] | None:
        return deepcopy(self._scope(scope)["ready"])

    @contextmanager
    def _persist(self) -> Iterator[None]:
        previous = deepcopy(self.data)
        try:
            yield
            self.flush()
        except BaseException:
            self.data = previous
            raise

    def save(self, scope: str, key: str, value: dict[str, Any]) -> None:
        with self._persist():
            self._scope(scope)["resources"][key] = deepcopy(value)

    def delete(self, scope: str, key: str) -> None:
        with self._persist():
            self._scope(scope)["resources"].pop(key, None)

    def mark_ready(self, scope: str, value: dict[str, Any] | None) -> None:
        with self._persist():
            self._scope(scope)["ready"] = deepcopy(value)

    def flush(self) -> None:
        pass


class FileStore(MemoryStore):
    """Atomic private JSON state with a separate, process-wide advisory lock file."""

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)
        self._locked = False

    @contextmanager
    def lock(self, scope: str) -> Iterator[None]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt
                    os.write(fd, b"0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    getattr(msvcrt, "locking")(fd, getattr(msvcrt, "LK_LOCK"), 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                self._locked = True
                if self.path.exists():
                    try:
                        data = json.loads(self.path.read_text(encoding="utf-8"))
                        if data.get("format_version") != "1" or not isinstance(data["scopes"], dict):
                            raise ValueError("unsupported state")
                        for value in data["scopes"].values():
                            if not isinstance(value["resources"], dict) or "ready" not in value:
                                raise ValueError("invalid scope")
                        self.data = data
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:
                        raise DefinitionError("Corrupt or unsupported definition state") from exc
                yield
            finally:
                self._locked = False
                os.close(fd)

    def flush(self) -> None:
        if not self._locked:
            raise DefinitionError("File state writes require a lock")
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class PostgresStore(MemoryStore):
    """Session advisory locks and transactional snapshots; schema setup is explicit.

    Each successful resource is committed separately, preserving progress when a later
    remote mutation fails. Constructors never connect or issue DDL.
    """

    schema_sql = """CREATE TABLE IF NOT EXISTS envai_definition_state (
        scope TEXT PRIMARY KEY, document JSONB NOT NULL
    )"""

    def __init__(self, dsn: str):
        super().__init__()
        self.dsn = dsn
        self.connection: Any = None
        self.scope: str | None = None

    def create_schema(self) -> None:
        import psycopg
        with psycopg.connect(self.dsn) as connection:
            connection.execute(self.schema_sql)

    @contextmanager
    def lock(self, scope: str) -> Iterator[None]:
        import psycopg
        with self._lock, psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (scope,))
            self.connection, self.scope = connection, scope
            try:
                row = connection.execute(
                    "SELECT document FROM envai_definition_state WHERE scope = %s", (scope,)
                ).fetchone()
                self.data = {"format_version": "1", "scopes": {}}
                if row:
                    document = row[0]
                    if (not isinstance(document, dict)
                            or not isinstance(document.get("resources"), dict)
                            or "ready" not in document):
                        raise DefinitionError("Corrupt definition state")
                    self.data["scopes"][scope] = document
                yield
            finally:
                self.connection, self.scope = None, None
                connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (scope,))

    def flush(self) -> None:
        if self.connection is None or self.scope is None:
            raise DefinitionError("PostgreSQL state writes require a lock")
        from psycopg.types.json import Jsonb
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO envai_definition_state(scope, document) VALUES (%s, %s) "
                "ON CONFLICT (scope) DO UPDATE SET document = EXCLUDED.document",
                (self.scope, Jsonb(self._scope(self.scope))),
            )


class SQLiteStore(MemoryStore):
    """Explicitly provisioned SQLite snapshots with a separate process lock.

    Resource writes commit independently while the process lock spans reconciliation,
    so a later error cannot roll back already recorded remote effects.
    """
    schema_sql = "CREATE TABLE IF NOT EXISTS envai_definition_state (scope TEXT PRIMARY KEY, document TEXT NOT NULL)"

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)
        self.connection: Any = None
        self.scope: str | None = None

    def create_schema(self) -> None:
        import sqlite3
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(self.schema_sql)

    @contextmanager
    def lock(self, scope: str) -> Iterator[None]:
        import sqlite3
        with self._lock:
            fd = os.open(str(self.path)+".lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt
                    os.write(fd,b"0")
                    os.lseek(fd,0,os.SEEK_SET)
                    getattr(msvcrt,"locking")(fd,getattr(msvcrt,"LK_LOCK"),1)
                else:
                    import fcntl
                    fcntl.flock(fd,fcntl.LOCK_EX)
                connection = sqlite3.connect(self.path.resolve().as_uri()+"?mode=rw",uri=True)
                try:
                    connection.execute("PRAGMA synchronous=FULL")
                    self.connection,self.scope = connection,scope
                    row=connection.execute("SELECT document FROM envai_definition_state WHERE scope=?",(scope,)).fetchone()
                    self.data={"format_version":"1","scopes":{}}
                    if row:
                        self.data["scopes"][scope]=_database_document(row[0])
                    yield
                finally:
                    self.connection,self.scope=None,None
                    connection.close()
            finally:
                os.close(fd)

    def flush(self) -> None:
        if self.connection is None or self.scope is None:
            raise DefinitionError("SQLite state writes require a lock")
        with self.connection:
            self.connection.execute(
                "INSERT INTO envai_definition_state(scope,document) VALUES (?,?) "
                "ON CONFLICT(scope) DO UPDATE SET document=excluded.document",
                (self.scope,json.dumps(self._scope(self.scope),ensure_ascii=False,allow_nan=False)))


def _database_document(value: Any) -> dict[str, Any]:
    try:
        document=json.loads(value)
        if (not isinstance(document,dict) or not isinstance(document.get("resources"),dict)
                or "ready" not in document):
            raise ValueError("invalid state")
    except (ValueError,TypeError) as exc:
        raise DefinitionError("Corrupt definition state") from exc
    return document


class MySQLStore(MemoryStore):
    """MySQL 8 snapshots and session locks; exact UTF-8 scope bytes, explicit DDL.

    Supply a mysql:// DSN or a callable returning a fresh DB-API connection with
    PyMySQL-compatible cursor/transaction methods. The store closes that connection.
    Use a factory when custom TLS or other connection policy is required.
    """
    schema_sql = """CREATE TABLE IF NOT EXISTS envai_definition_state (
        scope VARBINARY(2048) PRIMARY KEY, document JSON NOT NULL
    ) ENGINE=InnoDB"""

    def __init__(self, connection: Any):
        super().__init__()
        self._configuration=connection
        self.connection: Any=None
        self.scope: str | None=None

    def _connect(self):
        if callable(self._configuration):
            return self._configuration()
        from urllib.parse import unquote, urlsplit

        import pymysql
        parsed=urlsplit(self._configuration)
        if parsed.scheme!="mysql" or not parsed.hostname or not parsed.path.strip("/") or parsed.query or parsed.fragment:
            raise DefinitionError("Use a mysql:// DSN or a configured connection factory")
        return pymysql.connect(host=parsed.hostname,port=parsed.port or 3306,
            user=unquote(parsed.username or ""),password=unquote(parsed.password or ""),
            database=unquote(parsed.path.lstrip("/")),autocommit=True,charset="utf8mb4")

    @staticmethod
    def _check_schema(connection: Any) -> None:
        import re
        with connection.cursor() as cursor:
            cursor.execute("SHOW COLUMNS FROM envai_definition_state")
            columns = {row[0]: row for row in cursor.fetchall()}
            cursor.execute("SHOW INDEX FROM envai_definition_state WHERE Key_name='PRIMARY'")
            primary = cursor.fetchall()
            cursor.execute("SHOW CREATE TABLE envai_definition_state")
            definition = cursor.fetchone()[1]
        scope = columns.get("scope")
        document = columns.get("document")
        if (scope is None or scope[1].lower() != "varbinary(2048)" or scope[2] != "NO"
                or document is None or document[1].lower() != "json" or document[2] != "NO"
                or len(primary) != 1 or primary[0][4] != "scope" or primary[0][7] is not None
                or re.search(r"\bENGINE\s*=\s*InnoDB\b", definition, re.IGNORECASE) is None):
            raise DefinitionError("MySQL state requires explicit migration to InnoDB, exact VARBINARY(2048) primary scope and non-null JSON document")

    def create_schema(self) -> None:
        connection=self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(self.schema_sql)
            connection.commit()
            self._check_schema(connection)
        finally:
            connection.close()

    @contextmanager
    def lock(self, scope: str) -> Iterator[None]:
        import hashlib
        scope_bytes=scope.encode("utf-8")
        if not scope_bytes or len(scope_bytes)>2048:
            raise DefinitionError("MySQL scope must contain 1 to 2048 UTF-8 bytes")
        lock_name=hashlib.sha256(b"envai-definitions:"+scope_bytes).hexdigest()
        with self._lock:
            connection=self._connect()
            acquired=False
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT GET_LOCK(%s,30)",(lock_name,))
                    if cursor.fetchone()[0]!=1:
                        raise DefinitionError("Could not acquire MySQL definition lock")
                    acquired=True
                    self._check_schema(connection)
                    cursor.execute("SELECT document FROM envai_definition_state WHERE scope=%s",(scope_bytes,))
                    row=cursor.fetchone()
                self.connection,self.scope=connection,scope
                self.data={"format_version":"1","scopes":{}}
                if row:
                    self.data["scopes"][scope]=_database_document(row[0])
                yield
            finally:
                self.connection,self.scope=None,None
                try:
                    if acquired:
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT RELEASE_LOCK(%s)",(lock_name,))
                finally:
                    connection.close()

    def flush(self) -> None:
        if self.connection is None or self.scope is None:
            raise DefinitionError("MySQL state writes require a lock")
        document=json.dumps(self._scope(self.scope),ensure_ascii=False,allow_nan=False)
        self.connection.begin()
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("INSERT INTO envai_definition_state(scope,document) VALUES (%s,%s) "
                    "ON DUPLICATE KEY UPDATE document=VALUES(document)",(self.scope.encode("utf-8"),document))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
