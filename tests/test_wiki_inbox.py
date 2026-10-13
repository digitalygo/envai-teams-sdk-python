from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor

import pytest
from test_wiki_webhooks import VECTOR, signed, verifier

from envai import PostgresWikiWebhookInbox, ReceiptOutcome, SqliteWikiWebhookInbox
from envai.errors import WebhookStoreError, WebhookVerificationError


def receive_process(kind, target, body, headers):
    store = (
        PostgresWikiWebhookInbox(target) if kind == "postgres" else SqliteWikiWebhookInbox(target)
    )
    return store.receive(verifier(), body, headers, connection_scope="account")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("ENVAI_TEST_POSTGRES_DSN") or os.environ.get("TEST_DATABASE_URL")
        if not dsn:
            if os.environ.get("ENVAI_REQUIRE_POSTGRES") == "1":
                pytest.fail("CI requires a real disposable PostgreSQL database")
            pytest.skip("Set ENVAI_TEST_POSTGRES_DSN to run real PostgreSQL transaction tests")
        import psycopg

        with psycopg.connect(dsn) as db:
            db.execute("DROP TABLE IF EXISTS envai_wiki_outbox, envai_wiki_receipts CASCADE")
        result = PostgresWikiWebhookInbox(dsn)
    else:
        result = SqliteWikiWebhookInbox(tmp_path / "wiki.sqlite3")
    result.create_schema()
    return result


def test_durable_receipt_and_work_atomic_duplicate_collision_scoping(store):
    body, headers = signed()
    assert (
        store.receive(verifier(), body, headers, connection_scope="account")
        == ReceiptOutcome.ACCEPTED
    )
    assert (
        store.receive(verifier(), body, headers, connection_scope="account")
        == ReceiptOutcome.DUPLICATE
    )
    jobs = store.pending()
    assert len(jobs) == 1 and jobs[0].raw_body == body
    assert jobs[0].event_id == VECTOR["headers"]["Envai-Webhook-Id"]
    event = json.loads(body)
    event["source_revision"] += 1
    assert (
        store.receive(verifier(), *signed(event=event), connection_scope="account")
        == ReceiptOutcome.COLLISION
    )
    assert store.pending() == jobs
    assert (
        store.receive(verifier(), body, headers, connection_scope="other-account")
        == ReceiptOutcome.ACCEPTED
    )
    assert len(store.pending()) == 2
    store.complete(jobs[0].id)
    store.complete(jobs[0].id)
    assert len(store.pending()) == 1
    assert (
        store.receive(verifier(), body, headers, connection_scope="account")
        == ReceiptOutcome.DUPLICATE
    )
    assert len(store.pending()) == 1


def test_failure_rolls_back_receipt_and_outbox(store):
    # Force a real database insert failure after receipt insertion.
    db = store._connect()
    with db:
        db.execute("DROP TABLE envai_wiki_outbox")
    db.close()
    with pytest.raises(WebhookStoreError):
        store.receive(verifier(), *signed(), connection_scope="account")
    db = store._connect()
    with db:
        assert db.execute("SELECT COUNT(*) FROM envai_wiki_receipts").fetchone()[0] == 0
    db.close()
    store.create_schema()
    assert (
        store.receive(verifier(), *signed(), connection_scope="account") == ReceiptOutcome.ACCEPTED
    )
    assert len(store.pending()) == 1


def test_multiprocess_concurrent_receipt_has_one_job(store):
    target = store._conninfo if store.dialect == "postgres" else str(store.path)
    with ProcessPoolExecutor(6, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(receive_process, store.dialect, target, *signed()) for _ in range(6)]
        results = [f.result(timeout=30) for f in futures]
    assert results.count(ReceiptOutcome.ACCEPTED) == 1
    assert results.count(ReceiptOutcome.DUPLICATE) == 5
    assert len(store.pending()) == 1


async def test_async_intake_and_outbox(store):
    await store.create_schema_async()
    assert (
        await store.receive_async(verifier(True), *signed(), connection_scope="account")
        == ReceiptOutcome.ACCEPTED
    )
    assert (
        await store.receive_async(verifier(True), *signed(), connection_scope="account")
        == ReceiptOutcome.DUPLICATE
    )
    jobs = await store.pending_async()
    assert len(jobs) == 1
    await store.complete_async(jobs[0].id)
    assert await store.pending_async() == []


def test_verify_before_database(store, monkeypatch):
    def forbidden():
        raise AssertionError("Database was touched before verification")

    monkeypatch.setattr(store, "_connect", forbidden)
    body, headers = signed()
    with pytest.raises(WebhookVerificationError):
        store.receive(verifier(), body + b" ", headers, connection_scope="account")


@pytest.mark.parametrize("scope", ["", "a\0b", "a\nb"])
def test_scope_and_limits(store, scope):
    with pytest.raises(ValueError):
        store.receive(verifier(), *signed(), connection_scope=scope)
    for limit in (0, -1, 1001, True, 2.5):
        with pytest.raises(ValueError):
            store.pending(limit=limit)


@pytest.mark.parametrize("path", ["", ":memory:", "file:memory?mode=memory"])
def test_persistent_file_required(path):
    with pytest.raises(ValueError):
        SqliteWikiWebhookInbox(path)


async def test_cancelled_async_delivery_remains_retry_safe(store, monkeypatch):
    import threading

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    persist = store._persist

    def blocked(*args):
        started.set()
        release.wait(5)
        try:
            return persist(*args)
        finally:
            finished.set()

    monkeypatch.setattr(store, "_persist", blocked)
    task = asyncio.create_task(
        store.receive_async(verifier(True), *signed(), connection_scope="account")
    )
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    # Complete one racing retry; either may win, both share the durable key.
    result = await store.receive_async(verifier(True), *signed(), connection_scope="account")
    assert result in (ReceiptOutcome.ACCEPTED, ReceiptOutcome.DUPLICATE)
    assert await asyncio.to_thread(finished.wait, 5)
    assert len(store.pending()) == 1


def crash_process(kind, target, after_commit):
    store = (
        PostgresWikiWebhookInbox(target) if kind == "postgres" else SqliteWikiWebhookInbox(target)
    )
    if not after_commit:
        connect = store._connect

        class Interrupted:
            def __init__(self):
                self.db = connect()

            def __enter__(self):
                self.db.__enter__()
                return self

            def __exit__(self, *args):
                return self.db.__exit__(*args)

            def close(self):
                self.db.close()

            def execute(self, sql, params=()):
                cursor = self.db.execute(sql, params)
                if sql.startswith("INSERT INTO envai_wiki_receipts"):
                    os._exit(70)
                return cursor

        store._connect = Interrupted
    store.receive(verifier(), *signed(), connection_scope="account")
    os._exit(71)


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_crash_has_no_receipt_without_work(store, after_commit):
    target = store._conninfo if store.dialect == "postgres" else str(store.path)
    process = multiprocessing.get_context("spawn").Process(
        target=crash_process, args=(store.dialect, target, after_commit)
    )
    process.start()
    process.join(30)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("Crash fixture did not exit")
    assert process.exitcode == (71 if after_commit else 70)
    assert len(store.pending()) == (1 if after_commit else 0)
    result = store.receive(verifier(), *signed(), connection_scope="account")
    assert result == (ReceiptOutcome.DUPLICATE if after_commit else ReceiptOutcome.ACCEPTED)
    assert len(store.pending()) == 1


def test_postgres_optional_dependency_failure_is_explicit(monkeypatch):
    import builtins

    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "psycopg":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(WebhookStoreError):
        PostgresWikiWebhookInbox("unused")._connect()
