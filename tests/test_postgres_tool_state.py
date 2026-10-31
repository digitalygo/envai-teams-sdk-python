"""Real PostgreSQL contention and durable failure behavior; no provider calls."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from envai import PostgresToolState
from envai.errors import ToolStateError
from envai.postgres_tool_state import SCHEMA_SQL


@pytest.fixture
def postgres_dsn():
    dsn = os.environ.get("ENVAI_TEST_POSTGRES_DSN") or os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        if os.environ.get("ENVAI_REQUIRE_POSTGRES") == "1":
            pytest.fail("CI requires isolated PostgreSQL")
        pytest.skip("Set TEST_DATABASE_URL to an isolated disposable PostgreSQL database")
    import psycopg
    with psycopg.connect(dsn) as db:
        db.execute("DROP TABLE IF EXISTS envai_tool_claims")
    # Construction does not create tables or repair an absent schema.
    with PostgresToolState(dsn) as store, pytest.raises(psycopg.errors.UndefinedTable):
        store.get("scope", "chat", "call")
    with psycopg.connect(dsn) as db:
        db.execute(SCHEMA_SQL)
    return dsn


def test_postgres_concurrent_atomic_claim_and_resolved_barrier(postgres_dsn):
    gate = threading.Barrier(8)
    def claim(_):
        with PostgresToolState(postgres_dsn) as store:
            gate.wait(5)
            return store.claim("scope", "chat", "call", "fingerprint", "turn")
    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(claim, range(8))) == 1
    with PostgresToolState(postgres_dsn) as store:
        assert store.get("scope", "chat", "call").status == "running"
        store.transition("scope", "chat", "call", "ambiguous")
        store.resolve("scope", "chat", "call", "turn")
        store.transition("scope", "chat", "call", "submitted")
        assert store.get("scope", "chat", "call").status == "resolved"
        assert not store.claim("scope", "chat", "call", "fingerprint", "turn")
        assert store.claim("scope2", "chat", "call", "fingerprint", "turn")
        for fingerprint, turn in (("other", "turn"), ("fingerprint", "other")):
            with pytest.raises(ToolStateError):
                store.claim("scope", "chat", "call", fingerprint, turn)
        with pytest.raises(ToolStateError):
            store.resolve("scope", "chat", "call", "other")
        with pytest.raises(ToolStateError):
            store.transition("scope", "chat", "missing", "running")
        store.resolve("scope", "chat", "unseen", "turn")
        assert not store.claim("scope", "chat", "unseen", "new-fingerprint", "turn")


def test_postgres_corruption_fails_closed(postgres_dsn):
    import psycopg
    with psycopg.connect(postgres_dsn) as db:
        db.execute("INSERT INTO envai_tool_claims VALUES ('scope','chat','call','fingerprint','corrupt','turn')")
    with PostgresToolState(postgres_dsn) as store:
        for action in (
            lambda: store.get("scope", "chat", "call"),
            lambda: store.claim("scope", "chat", "call", "fingerprint", "turn"),
            lambda: store.resolve("scope", "chat", "call", "turn"),
            lambda: store.transition("scope", "chat", "call", "submitted"),
        ):
            with pytest.raises(ToolStateError):
                action()
    with psycopg.connect(postgres_dsn) as db:
        assert db.execute("SELECT status FROM envai_tool_claims").fetchone()[0] == "corrupt"


def test_postgres_runner_persistent_task_barriers(postgres_dsn, tool_event):
    import httpx
    from negotiated_transport import negotiated_transport
    from test_background import JWKS, ping, task_terminal, wrapped

    from envai import BackgroundClientToolRunner, ClientToolRegistry, EnvaiClient, JwksVerifier
    effects = []
    registry = ClientToolRegistry()
    registry.register(tool_event.data.tool_name, lambda a,c: effects.append(1) or "ok", {"type":"object"})
    with EnvaiClient("test", native_tasks=True, transport=negotiated_transport(lambda r: httpx.Response(204))) as client:
        for restart in (False, True):
            with PostgresToolState(postgres_dsn) as store, BackgroundClientToolRunner(client,
                    registry, store=store, scope="test", chat_id=tool_event.chat_id,
                    verifier=JwksVerifier(lambda: JWKS)) as runner:
                runner.consume(ping(tool_event))
                if not restart:
                    runner.consume(task_terminal(tool_event))
                else:
                    runner.consume(wrapped(tool_event))
                    runner.wait()
    assert effects == []
