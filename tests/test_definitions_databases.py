"""Real SQLite/MySQL state portability, exact account identity and crash recovery."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from test_definitions_parity import GOLDEN, Remote

from envai.definitions import (
    DefinitionError,
    Definitions,
    FileStore,
    Manifest,
    ManifestDocument,
    MySQLStore,
    SQLiteStore,
    SyncOptions,
)


@pytest.fixture(params=["sqlite", "mysql"])
def database_store(request,tmp_path):
    if request.param=="sqlite":
        config=str(tmp_path/"definitions.sqlite")
        def factory():
            return SQLiteStore(config)
        assert not os.path.exists(config)
    else:
        config=os.environ.get("TEST_MYSQL_DSN")
        if not config:
            if os.environ.get("ENVAI_REQUIRE_MYSQL")=="1":
                pytest.fail("CI requires a real isolated MySQL database")
            pytest.skip("Set TEST_MYSQL_DSN for real MySQL state tests")
        def factory():
            return MySQLStore(config)
    store=factory()
    store.create_schema()
    return factory,"test-"+str(uuid4())


def test_database_roundtrip_scopes_and_concurrent_workers(database_store):
    factory,prefix=database_store
    for scope in (prefix+"Foo",prefix+"foo",prefix+"fóo",prefix+"Foo ",prefix+"fóo "):
        with factory().lock(scope):
            pass
        store=factory()
        with store.lock(scope):
            store.save(scope,"tool:item",{"scope":scope,"literal":{"empty":{},"list":[],"none":None}})
        restarted=factory()
        with restarted.lock(scope):
            assert restarted.resources(scope)["tool:item"]["scope"]==scope
            assert restarted.resources(scope)["tool:item"]["literal"]=={"empty":{},"list":[],"none":None}
    scope=prefix+"counter"
    def increment(_):
        store=factory()
        with store.lock(scope):
            count=store.resources(scope).get("counter",{}).get("value",0)+1
            time.sleep(0.002)
            store.save(scope,"counter",{"value":count})
            return count
    with ThreadPoolExecutor(8) as pool:
        assert sorted(pool.map(increment,range(8)))==list(range(1,9))
    store=factory()
    with store.lock(scope):
        assert store.resources(scope)["counter"]["value"]==8
        with pytest.raises(ValueError):
            store.save(scope,"invalid",{"nan":float("nan")})
    restarted=factory()
    with restarted.lock(scope):
        assert "invalid" not in restarted.resources(scope)
        restarted.mark_ready(scope,{"manifest_hash":"fixture"})
        restarted.delete(scope,"counter")
    with factory().lock(scope):
        pass
    with pytest.raises(DefinitionError):
        restarted.save(scope,"outside",{})


def test_database_corruption_fails_closed(database_store):
    factory,scope=database_store
    store=factory()
    with store.lock(scope):
        store.save(scope,"ok",{})
        if isinstance(store,SQLiteStore):
            store.connection.execute("UPDATE envai_definition_state SET document='[]' WHERE scope=?",(scope,))
            store.connection.commit()
        else:
            with store.connection.cursor() as cursor:
                cursor.execute("UPDATE envai_definition_state SET document='[]' WHERE scope=%s",(scope.encode(),))
            store.connection.commit()
    with pytest.raises(DefinitionError),factory().lock(scope):
        pass


@pytest.mark.parametrize("kind",["agent","team"])
def test_landed_create_crash_recovers_unique_missing_identity(kind,tmp_path):
    source=deepcopy(GOLDEN["dependency_order"]["manifest"])
    remote=Remote()
    path=tmp_path/"state.json"
    service=Definitions(remote,FileStore(path),"scope")
    original=remote.create
    def crash(resource,payload):
        value=original(resource,payload)
        if resource==kind:
            raise KeyboardInterrupt("landed before receipt")
        return value
    remote.create=crash
    with pytest.raises(KeyboardInterrupt):
        service.sync(source)
    created_ids=[x["id"] for x in remote.values[kind]]
    assert len(created_ids)==1
    remote.create=original
    remote.calls.clear()
    restarted=Definitions(remote,FileStore(path),"scope")
    assert restarted.sync(source)["ready"]
    assert [x["id"] for x in remote.values[kind]][:1]==created_ids
    assert not any(call[0]=="create" and call[1]==kind and call[2]["name"]==remote.values[kind][0]["name"] for call in remote.calls)


@pytest.mark.parametrize("failure",["catalog","list","duplicate"])
def test_real_sync_invalidates_readiness_on_prepare_failure(failure,tmp_path):
    source=deepcopy(GOLDEN["dependency_order"]["manifest"])
    remote=Remote()
    service=Definitions(remote,FileStore(tmp_path/"state.json"),"scope")
    assert service.sync(source)["ready"]
    assert service.ready(source)
    if failure=="catalog":
        remote.catalog_failure=True
    elif failure=="list":
        remote.read_failure=True
    else:
        remote.values["agent"].append(deepcopy(remote.values["agent"][0]))
    with pytest.raises((DefinitionError,ConnectionError)):
        service.plan(source)
    assert service.ready(source)  # Read-only planning cannot overwrite deployment state.
    with pytest.raises((DefinitionError,ConnectionError)):
        service.sync(source,SyncOptions(dry_run=True))
    assert service.ready(source)
    with pytest.raises((DefinitionError,ConnectionError)):
        service.sync(source)
    assert not service.ready(source)


def test_native_typed_manifest_mapping():
    document:ManifestDocument={"format_version":"1","agents":{"primary":{
        "name":"Primary","system_prompt":"Work","mode":"primary"}}}
    assert Manifest(document).document["agents"]["primary"]["name"]=="Primary"


def test_database_constructors_do_not_connect_or_create(tmp_path):
    count=[]
    mysql=MySQLStore(lambda:count.append(1))
    assert count==[]
    with pytest.raises(DefinitionError):
        with mysql.lock(""):
            pass
    assert count==[]
    sqlite=SQLiteStore(tmp_path/"missing.sqlite")
    assert not sqlite.path.exists()
    with pytest.raises(DefinitionError):
        sqlite.save("scope","resource",{})


def test_database_flush_rolls_back_after_sql_was_executed(database_store):
    factory,scope=database_store
    store=factory()
    with store.lock(scope):
        store.save(scope,"before",{"value":"durable"})
        raw=store.connection
        class Cursor:
            def __init__(self,cursor):
                self.cursor=cursor
            def __enter__(self):
                self.cursor.__enter__()
                return self
            def __exit__(self,*args):
                return self.cursor.__exit__(*args)
            def execute(self,sql,*args):
                result=self.cursor.execute(sql,*args)
                if sql.startswith("INSERT"):
                    raise RuntimeError("after database write before commit")
                return result
            def __getattr__(self,name):
                return getattr(self.cursor,name)
        class Connection:
            def __getattr__(self,name):
                return getattr(raw,name)
            def __enter__(self):
                raw.__enter__()
                return self
            def __exit__(self,*args):
                return raw.__exit__(*args)
            def cursor(self):
                return Cursor(raw.cursor())
            def execute(self,sql,*args):
                result=raw.execute(sql,*args)
                if sql.startswith("INSERT"):
                    raise RuntimeError("after database write before commit")
                return result
        store.connection=Connection()
        try:
            with pytest.raises(RuntimeError):
                store.save(scope,"uncommitted",{"value":"must roll back"})
        finally:
            store.connection=raw
        assert store.resources(scope)=={"before":{"value":"durable"}}
        store.save(scope,"after",{"value":"safe following write"})
    restarted=factory()
    with restarted.lock(scope):
        assert restarted.resources(scope)=={"before":{"value":"durable"},"after":{"value":"safe following write"}}


@pytest.mark.parametrize("schema,engine", [
    ("scope VARCHAR(255) PRIMARY KEY, document JSON NOT NULL", "InnoDB"),
    ("scope VARBINARY(2048) NOT NULL, document JSON NOT NULL", "InnoDB"),
    ("scope VARBINARY(2048) PRIMARY KEY, document JSON", "InnoDB"),
    ("scope VARBINARY(2048) NOT NULL, document JSON NOT NULL, PRIMARY KEY(scope(200))", "InnoDB"),
    ("scope VARBINARY(2048) NOT NULL, document JSON NOT NULL, PRIMARY KEY(scope(200))", "MyISAM"),
])
def test_mysql_rejects_legacy_collated_identity_schema(schema,engine):
    dsn=os.environ.get("TEST_MYSQL_DSN")
    if not dsn:
        if os.environ.get("ENVAI_REQUIRE_MYSQL")=="1":
            pytest.fail("CI requires MySQL")
        pytest.skip("Set TEST_MYSQL_DSN")
    def legacy_connection():
        connection=MySQLStore(dsn)._connect()
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMPORARY TABLE envai_definition_state "
                "("+schema+") ENGINE="+engine)
        return connection
    store=MySQLStore(legacy_connection)
    with pytest.raises(DefinitionError,match="migration"):
        store.create_schema()
    with pytest.raises(DefinitionError,match="migration"),store.lock("Foo"):
        pass


def test_sqlite_literal_uri_characters_are_not_reinterpreted(tmp_path):
    path=tmp_path/"definitions ?#%.sqlite"
    store=SQLiteStore(path)
    store.create_schema()
    with store.lock("scope"):
        store.save("scope","key",{"literal":"correct file"})
    assert path.exists()
    restarted=SQLiteStore(path)
    with restarted.lock("scope"):
        assert restarted.resources("scope")=={"key":{"literal":"correct file"}}


@pytest.mark.parametrize("operation",["save","delete","ready"])
def test_store_in_memory_rollback_before_following_write(operation,tmp_path,monkeypatch):
    store=FileStore(tmp_path/"state.json")
    with store.lock("scope"):
        store.save("scope","before",{"value":1})
        old=deepcopy(store.data)
        original=store.flush
        def failed():
            raise RuntimeError("failed persistence")
        monkeypatch.setattr(store,"flush",failed)
        with pytest.raises(RuntimeError):
            if operation=="save":
                store.save("scope","failed",{})
            elif operation=="delete":
                store.delete("scope","before")
            else:
                store.mark_ready("scope",{"manifest_hash":"not durable"})
        assert store.data==old
        monkeypatch.setattr(store,"flush",original)
        store.save("scope","after",{})
    restarted=FileStore(store.path)
    with restarted.lock("scope"):
        assert restarted.resources("scope")=={"before":{"value":1},"after":{}}
        assert restarted.ready("scope") is None
