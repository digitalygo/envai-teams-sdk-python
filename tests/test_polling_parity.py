import asyncio
import threading
import time

import httpx
import pytest
from negotiated_transport import ACK
from test_http import sample

from envai import AsyncEnvaiClient, EnvaiClient, RequestOptions, models
from envai.errors import CancelledError, ProtocolError, TransportError, ValidationError


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("kind,state,lifecycle,recoverable", [
    ("source","completed","inbox",False), ("source","not_required","raw",False),
    ("source","failed","inbox",False), ("source","running","deleted",False),
    ("source","running","outbox",False), ("operation","completed",None,None),
    ("operation","failed",None,None),
])
async def test_wiki_wait_terminal_states(async_, kind, state, lifecycle, recoverable):
    seen = []
    from envai._schema import OPERATIONS
    op = OPERATIONS["getWikiSource" if kind == "source" else "getWikiOperation"]
    payload = sample(op["response"])
    envelope = payload
    payload = payload.get("data", payload)
    payload.update(workspace_id="workspace")
    payload["file_id" if kind == "source" else "operation_id"] = "id"
    if kind == "source":
        payload.update(lifecycle=lifecycle, conversion={"status":state,"recoverable":recoverable})
    else:
        payload["status"] = state
    def http(request):
        seen.append(request)
        return httpx.Response(200, headers=ACK, json=envelope)
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", transport=httpx.MockTransport(http))
    method = getattr(client.wiki, "wait_"+kind)
    value = await method("workspace","id") if async_ else method("workspace","id")
    assert value.workspace_id == "workspace" and len(seen) == 1 and seen[0].method == "GET"
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("kind", ["source", "operation"])
async def test_wiki_wait_identity_deadline_cancel(async_, kind):
    from envai._schema import OPERATIONS
    payload = sample(OPERATIONS["getWikiSource" if kind == "source" else "getWikiOperation"]["response"])
    envelope = payload
    payload = payload.get("data", payload)
    payload.update(workspace_id="wrong")
    payload["file_id" if kind == "source" else "operation_id"] = "id"
    if kind == "source":
        payload.update(lifecycle="uploaded",conversion={"status":"failed","recoverable":True})
    else:
        payload["status"] = "running"
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", transport=httpx.MockTransport(
        lambda r:httpx.Response(200,headers=ACK,json=envelope)))
    method = getattr(client.wiki,"wait_"+kind)
    with pytest.raises(ProtocolError):
        await method("workspace","id") if async_ else method("workspace","id")
    payload["workspace_id"] = "workspace"
    with pytest.raises((TransportError,TimeoutError)):
        await method("workspace","id",timeout=0.01,poll_interval=0.002) if async_ else method("workspace","id",timeout=0.01,poll_interval=0.002)
    cancel = threading.Event()
    timer = threading.Timer(0.01,cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        with pytest.raises(CancelledError):
            await method("workspace","id",cancel=cancel,poll_interval=10) if async_ else method("workspace","id",cancel=cancel,poll_interval=10)
        assert time.monotonic()-started < 0.2
    finally:
        timer.join()
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_async_request_cancellation_and_deadline(async_):
    cancel = threading.Event()
    seen = []
    async def http(request):
        seen.append(request)
        await asyncio.Event().wait()
    if not async_:
        for options in (RequestOptions(timeout=0.005),RequestOptions(deadline=time.monotonic()+0.005)):
            def sync(request):
                seen.append(request)
                raise httpx.ReadTimeout("transport timeout respected")
            with EnvaiClient("test",transport=httpx.MockTransport(sync)) as client, pytest.raises(TransportError):
                client.create_workspace(body={"name":"workspace"},request_options=options)
        assert len(seen) == 2  # No mutation retries.
        return
    async with AsyncEnvaiClient("test",transport=httpx.MockTransport(http)) as client:
        with pytest.raises(TimeoutError):
            await client.list_workspaces(request_options=RequestOptions(deadline=time.monotonic()+0.01))
        timer = threading.Timer(0.01,cancel.set)
        timer.start()
        try:
            with pytest.raises(CancelledError):
                await client.list_workspaces(request_options=RequestOptions(cancel=cancel))
        finally:
            timer.join()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("count", [0,1,3,5])
async def test_diagnostic_multipart_arrays(async_,count):
    seen = []
    from envai._schema import OPERATIONS
    payload = sample(OPERATIONS["testGenerateImage"]["response"])
    def http(request):
        seen.append(request)
        body = request.read()
        assert body.count(b'name="files[]"') == count
        for index in range(count):
            assert f"image-{index}.png".encode() in body
        assert b'name="prompt"' in body and b'name="model_code"' in body
        return httpx.Response(200,json=payload)
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", transport=httpx.MockTransport(http))
    kwargs = dict(files={"files":[(f"image-{i}.png",b"fixture","image/png") for i in range(count)]},
                  data={"prompt":"describe","model_code":"model"})
    try:
        if count in (0,5):
            with pytest.raises(ValidationError):
                await client.test_generate_image(**kwargs) if async_ else client.test_generate_image(**kwargs)
            assert not seen
        else:
            await client.test_generate_image(**kwargs) if async_ else client.test_generate_image(**kwargs)
            assert len(seen)==1
    finally:
        if async_:
            await client.aclose()
        else:
            client.close()


def test_update_models_preserve_fields_and_omission():
    cases = [
        (models.WorkspaceUpdateRequest(name="renamed"),{"name":"renamed"}),
        (models.UserToolUpdateRequest(description=None),{"description":None}),
        (models.AgentUpdateRequest(model=None,tools_enabled=[]),{"model":None,"tools_enabled":[]}),
        (models.TeamUpdateRequest(description=None,members=[]),{"description":None,"members":[]}),
    ]
    for model,payload in cases:
        assert model.model_dump(exclude_unset=True)==payload
        assert set(payload) <= set(type(model).model_fields)
    with EnvaiClient("test",transport=httpx.MockTransport(lambda r:pytest.fail("invalid update dispatched"))) as client:
        with pytest.raises(ValidationError):
            client.agents.update("id",body=models.AgentUpdateRequest())
        with pytest.raises(ValidationError):
            client.agents.update("id",body=models.AgentUpdateRequest(name=None))


@pytest.mark.parametrize("async_", [False,True])
async def test_custom_headers_preserve_auth_and_native(async_):
    seen=[]
    def http(request):
        seen.append(request)
        assert request.headers["X-Application"]=="integration"
        assert request.headers["Authorization"]=="Bearer secret"
        assert request.headers["X-Envai-Extensions"]=="native-tasks-v1"
        return httpx.Response(200,json={"data":[]})
    client=(AsyncEnvaiClient if async_ else EnvaiClient)("secret",native_tasks=True,headers={"X-Application":"integration"},transport=httpx.MockTransport(http))
    await client.list_workspaces() if async_ else client.list_workspaces()
    assert len(seen)==1 and "secret" not in repr(client)
    if async_:
        await client.aclose()
    else:
        client.close()
    for name in ("authorization","X-Envai-Extensions","X-Envai-Contract-Version"):
        with pytest.raises(ValueError):
            EnvaiClient("secret",headers={name:"override"})
