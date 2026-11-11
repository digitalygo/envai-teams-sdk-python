"""Offline independent jobs, originless delivery and finite choice contracts."""

import httpx
import pytest
from negotiated_transport import CAPABILITIES

from envai import AsyncEnvaiClient, EnvaiClient
from envai.errors import CompatibilityError, ProtocolError, TransportError

FEATURES = ["detached_background", "independent_jobs", "finite_choice_classification",
            "originless_deliveries", "native_deliveries", "strict_model"]
ACK = {"X-Envai-Contract-Version": "3", "X-Envai-Extensions": "native-tasks-v1"}
CHOICE = {"request_id": "choice-1", "context": {"output": "full"}, "question": "Next?",
          "choices": [{"label": "Continue", "description": "Ready"},
                      {"label": "Ask", "description": "Missing input"}]}
RESULT = {"request_id": "choice-1", "choice": "Continue", "model": "configured", "invocation_id": "i"}
TASK = {"task_id": "task", "origin_turn_id": "", "target_chat_id": "chat",
        "status": "completed", "output": "final", "result_turn_id": "final-turn"}
JOB = {"workspace_id": "w", "request_id": "job-1", "prompt": "Work", "strict_model": True}
DELIVERY = {"workspace_id": "w", "event_id": "notice", "task_id": "task",
            "status": "completed", "output": "full", "strict_model": True}


def transport(handler, seen, features=FEATURES, ack=ACK):
    def respond(request):
        assert request.headers["X-Envai-Extensions"] == "native-tasks-v1"
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={**CAPABILITIES, "features": features}, headers=ACK)
        seen.append(request)
        response = handler(request)
        response.headers.update(ack)
        return response
    return httpx.MockTransport(respond)


@pytest.mark.parametrize("async_", [False, True])
async def test_native_routes_and_final_job_wait(async_):
    seen = []
    def handler(request):
        if request.url.path.endswith('/jobs'):
            return httpx.Response(202, json={"task_id": "task", "turn_id": "initial-turn"})
        if request.url.path.endswith('/classify-choice'):
            return httpx.Response(200, json=RESULT)
        if request.url.path.endswith('/deliveries'):
            return httpx.Response(202, json={"event_id": "notice", "status": "queued", "duplicate": False})
        return httpx.Response(200, json=TASK)
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True, transport=transport(handler, seen))
    async def call(method, *args, **kwargs):
        result = method(*args, **kwargs)
        return await result if async_ else result
    receipt = await call(client.chats.start_job, "chat", body=JOB)
    assert receipt.turn_id == "initial-turn"
    result = await call(client.chats.classify_choice, "chat", body=CHOICE)
    assert result.choice == "Continue"
    await call(client.chats.deliver, "chat", body=DELIVERY)
    await call(client.chats.deliver, "chat", body={**DELIVERY, "origin_turn_id": ""})
    final = await call(client.chats.wait_for_independent_job, "chat", "task", timeout=1, poll_interval=.001)
    assert final.output == "final" and final.result_turn_id != receipt.turn_id
    assert all(request.url.path.startswith('/api/v1/atp/v3/chats/chat/') for request in seen)
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("method,body,missing", [("start_job", JOB, "independent_jobs"),
    ("start_job", JOB, "strict_model"), ("classify_choice", CHOICE, "finite_choice_classification"),
    ("deliver", DELIVERY, "originless_deliveries"), ("deliver", DELIVERY, "strict_model")])
def test_missing_capability_never_mutates(method, body, missing):
    seen = []
    with EnvaiClient("test", native_tasks=True, transport=transport(lambda r: pytest.fail('mutation'), seen,
            [feature for feature in FEATURES if feature != missing])) as client:
        with pytest.raises(CompatibilityError):
            getattr(client.chats, method)("chat", body=body)
    assert not seen


@pytest.mark.parametrize("patch", [{"context": None}, {"question": " "},
    {"question": "é" * 5000}, {"request_id": " "}, {"context": "x" * (1 << 20)},
    {"choices": [{"label": "Same", "description": "x"}] * 2},
    {"choices": [{"label": " edge", "description": "x"}, CHOICE['choices'][1]]},
    {"choices": [{"label": "bad\nlabel", "description": "x"}, CHOICE['choices'][1]]},
    {"choices": [{"label": "é"*100, "description": "x"}, CHOICE['choices'][1]]},
    {"choices": [{"label": "Ok", "description": " "}, CHOICE['choices'][1]]},
    {"choices": [{"label": "Ok", "description": "é"*3000}, CHOICE['choices'][1]]}])
def test_invalid_choices_fail_before_network(patch):
    with EnvaiClient("test", native_tasks=True, transport=httpx.MockTransport(lambda r: pytest.fail('network'))) as client:
        with pytest.raises(ValueError):
            client.chats.classify_choice("chat", body={**CHOICE, **patch})


@pytest.mark.parametrize("method,body,response", [("start_job", JOB, {"task_id": "", "turn_id": "t"}),
    ("start_job", JOB, {"task_id": "task", "turn_id": ""}),
    *[("classify_choice", CHOICE, {**RESULT, **patch}) for patch in [{"choice": "Invented"},
       {"request_id": "other"}, {"model": ""}, {"invocation_id": ""}]]])
def test_response_identity_is_closed(method, body, response):
    seen = []
    with EnvaiClient("test", native_tasks=True, transport=transport(lambda r: httpx.Response(200,json=response), seen)) as client:
        with pytest.raises(ProtocolError):
            getattr(client.chats, method)("chat", body=body)
    assert len(seen) == 1


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("patch", [{"task_id": "wrong"}, {"origin_turn_id": "other"}, {"target_chat_id": "other"}])
async def test_independent_wait_rejects_other_identity(async_, patch):
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True,
        transport=transport(lambda r: httpx.Response(200,json={**TASK, **patch}), []))
    with pytest.raises(ProtocolError):
        result = client.chats.wait_for_independent_job("chat", "task", timeout=.2)
        if async_:
            await result
    if async_:
        await client.aclose()
    else:
        client.close()


@pytest.mark.parametrize("method,body", [("start_job",JOB),("classify_choice",CHOICE),("deliver",DELIVERY)])
def test_uncertain_mutations_are_never_retried(method, body):
    seen=[]
    def lost(request):
        raise httpx.ReadError('lost')
    with EnvaiClient("test", native_tasks=True, transport=transport(lost,seen)) as client:
        with pytest.raises(TransportError):
            getattr(client.chats,method)("chat",body=body)
    assert len(seen)==1


def test_empty_origin_requires_capability_and_ack():
    with EnvaiClient("test", native_tasks=True, transport=transport(lambda r: httpx.Response(200,json=TASK),[],
            [feature for feature in FEATURES if feature!='independent_jobs'])) as client:
        with pytest.raises(CompatibilityError):
            client.chats.get_task("chat","task")
    with EnvaiClient("test", native_tasks=True, transport=transport(lambda r: httpx.Response(200,json=RESULT),[],ack={})) as client:
        with pytest.raises(CompatibilityError):
            client.chats.classify_choice("chat",body=CHOICE)
    with EnvaiClient("test") as client:
        with pytest.raises(CompatibilityError):
            client.chats.start_job("chat",body=JOB)
        with pytest.raises(CompatibilityError):
            client.chats.wait_for_independent_job("chat","task")


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("with_cancel", [False, True])
async def test_independent_wait_polls_until_final_without_mutation(async_, with_cancel):
    import threading

    seen = []
    def handler(request):
        return httpx.Response(200, json={**TASK, "status": "running" if len(seen) == 1 else "completed"})
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("test", native_tasks=True,
        transport=transport(handler, seen))
    result = client.chats.wait_for_independent_job("chat", "task", timeout=1,
        poll_interval=.001, cancel=threading.Event() if with_cancel else None)
    if async_:
        result = await result
        await client.aclose()
    else:
        client.close()
    assert result.output == "final" and len(seen) == 2
    assert all(request.method == "GET" for request in seen)


async def test_async_wait_requires_opt_in_and_feature():
    async with AsyncEnvaiClient("test") as client:
        with pytest.raises(CompatibilityError):
            await client.chats.wait_for_independent_job("chat", "task")
    async with AsyncEnvaiClient("test", native_tasks=True,
            transport=transport(lambda r: pytest.fail("task read"), [], [])) as client:
        with pytest.raises(CompatibilityError):
            await client.chats.wait_for_independent_job("chat", "task")
