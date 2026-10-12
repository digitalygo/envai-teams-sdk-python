"""Pre-existing local corruption must never be interpreted as an absent claim."""

import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_tools import setup_async, setup_sync

from envai import MemoryToolState, SQLiteToolState
from envai.errors import ToolStateError
from envai.tool_state import ToolRecord

KEY = ("account", "chat", "call")
OPERATIONS = ("claim", "get", "transition", "resolve")
BAD_RECORDS = [
    ToolRecord("", "running", "turn"),
    ToolRecord("", "submitted", "turn"),
    ToolRecord("hash", "unknown", "turn"),
    ToolRecord("hash", "", "turn"),
    ToolRecord("hash", "running", ""),
    ToolRecord(None, "resolved", "turn"),
    ToolRecord(b"hash", "running", "turn"),
    ToolRecord("hash", None, "turn"),
    ToolRecord("hash", "running", None),
]


def invoke(store, operation):
    args = {
        "claim": ("hash", "turn"),
        "get": (),
        "transition": ("ambiguous",),
        "resolve": ("turn",),
    }
    return getattr(store, operation)(*KEY, *args[operation])


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("bad", [None, False, {}, [], "record", *BAD_RECORDS])
def test_memory_corruption_is_not_absence_and_is_never_repaired(operation, bad):
    store = MemoryToolState()
    store._records[KEY] = bad
    with pytest.raises(ToolStateError):
        invoke(store, operation)
    assert store._records[KEY] is bad


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("bad", BAD_RECORDS)
def test_sqlite_corruption_is_never_repaired(tmp_path, operation, bad):
    path = str(tmp_path / "damaged.sqlite3")
    # Model a damaged/imported database that lacks the SDK's NOT NULL guards.
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE tool_claims (scope TEXT, chat_id TEXT, call_id TEXT, "
            "fingerprint TEXT, status TEXT, turn_id TEXT, PRIMARY KEY(scope,chat_id,call_id))"
        )
        db.execute(
            "INSERT INTO tool_claims VALUES (?,?,?,?,?,?)",
            (*KEY, bad.fingerprint, bad.status, bad.turn_id),
        )
    original = (tmp_path / "damaged.sqlite3").read_bytes()
    with SQLiteToolState(path) as store:
        before = store._db.execute("SELECT * FROM tool_claims").fetchall()
        with pytest.raises(ToolStateError):
            invoke(store, operation)
        assert store._db.execute("SELECT * FROM tool_claims").fetchall() == before
    assert (tmp_path / "damaged.sqlite3").read_bytes() == original


@pytest.mark.parametrize("operation", OPERATIONS)
def test_duplicate_sqlite_rows_fail_closed(tmp_path, operation):
    path = str(tmp_path / "duplicates.sqlite3")
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE tool_claims (scope, chat_id, call_id, fingerprint, status, turn_id)"
        )
        for _ in range(2):
            db.execute(
                "INSERT INTO tool_claims VALUES (?,?,?,?,?,?)", (*KEY, "hash", "running", "turn")
            )
    with SQLiteToolState(path) as store:
        with pytest.raises(ToolStateError):
            invoke(store, operation)
        assert store._db.execute("SELECT count(*) FROM tool_claims").fetchone()[0] == 2


@pytest.mark.parametrize("factory", [MemoryToolState, lambda: SQLiteToolState(":memory:")])
def test_invalid_inputs_and_cross_turn_resolution_fail_without_mutation(factory):
    store = factory()
    try:
        for invalid in (None, "", False, 1):
            for index in range(5):
                args = [*KEY, "hash", "turn"]
                args[index] = invalid
                with pytest.raises(ToolStateError):
                    store.claim(*args)
        assert store.get(*KEY) is None
        store.claim(*KEY, "hash", "turn")
        original = store.get(*KEY)
        with pytest.raises(ToolStateError):
            store.transition(*KEY, "unrecognized")
        with pytest.raises(ToolStateError):
            store.resolve(*KEY, "different-turn")
        assert store.get(*KEY) == original
        store.resolve(*KEY, "turn")
        store.resolve(*KEY, "turn")
        assert store.get(*KEY) == ToolRecord("hash", "resolved", "turn")
    finally:
        if isinstance(store, SQLiteToolState):
            store.close()


def test_sqlite_atomic_claim_across_independent_connections(tmp_path):
    path = str(tmp_path / "concurrent.sqlite3")
    with SQLiteToolState(path):
        pass

    def claim(_):
        with SQLiteToolState(path) as store:
            return store.claim(*KEY, "hash", "turn")

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(claim, range(32))) == 1


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("factory", ["memory", "sqlite"])
async def test_corrupt_claim_blocks_signed_runner(tool_event, async_, factory):
    called = []

    def handler(args, ctx):
        called.append(1)
        return "ok"

    async def async_handler(args, ctx):
        return handler(args, ctx)

    client, runner, store, sent = (
        await setup_async(tool_event, async_handler) if async_ else setup_sync(tool_event, handler)
    )
    key = (runner.scope, runner.chat_id, tool_event.data.call_id)
    if factory == "memory":
        store._records[key] = None
        before = copy.deepcopy(store._records)
    else:
        store = runner.store = SQLiteToolState(":memory:")
        store._db.execute(
            "INSERT INTO tool_claims VALUES (?,?,?,?,?,?)", (*key, "", "running", runner.turn_id)
        )
        store._db.commit()
        before = store._db.execute("SELECT * FROM tool_claims").fetchall()
    try:
        with pytest.raises(ToolStateError):
            if async_:
                await runner.consume(tool_event)
            else:
                runner.consume(tool_event)
        assert not called and not sent
        after = (
            store._records
            if factory == "memory"
            else store._db.execute("SELECT * FROM tool_claims").fetchall()
        )
        assert after == before
    finally:
        if async_:
            await runner.aclose()
            await client.aclose()
        else:
            runner.close()
            client.close()
        if factory == "sqlite":
            store.close()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("factory", ["memory", "sqlite"])
@pytest.mark.parametrize("damage", ["malformed", "missing"])
async def test_corruption_before_output_prevents_submission(tool_event, async_, factory, damage):
    called = []
    state = None
    key = None

    def handler(args, ctx):
        called.append(1)
        if factory == "memory":
            if damage == "missing":
                del state._records[key]
            else:
                state._records[key] = None
        else:
            query = (
                "DELETE FROM tool_claims"
                if damage == "missing"
                else "UPDATE tool_claims SET fingerprint=''"
            )
            state._db.execute(query)
            state._db.commit()
        return "ok"

    async def async_handler(args, ctx):
        return handler(args, ctx)

    client, runner, state, sent = (
        await setup_async(tool_event, async_handler) if async_ else setup_sync(tool_event, handler)
    )
    key = (runner.scope, runner.chat_id, tool_event.data.call_id)
    if factory == "sqlite":
        state = runner.store = SQLiteToolState(":memory:")
    try:
        if async_:
            await runner.consume(tool_event)
            with pytest.raises(ToolStateError):
                await runner.wait()
        else:
            runner.consume(tool_event)
            with pytest.raises(ToolStateError):
                runner.wait()
        assert called == [1] and not sent
        if damage == "missing":
            assert state.get(*key) is None
        else:
            with pytest.raises(ToolStateError):
                state.get(*key)
    finally:
        if async_:
            await runner.aclose()
            await client.aclose()
        else:
            runner.close()
            client.close()
        if factory == "sqlite":
            state.close()
