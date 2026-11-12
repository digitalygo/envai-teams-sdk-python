"""Project-key resource operations, cleanup envelopes, and conservative mutations."""

from __future__ import annotations

import inspect
import json
import threading

import httpx
import pytest
from test_http import sample, snake

from envai import AsyncEnvaiClient, EnvaiClient, RequestOptions, RetryPolicy, models
from envai._schema import OPERATIONS
from envai.errors import (
    CancelledError,
    ConflictError,
    PaymentRequiredError,
    ServerError,
    ValidationError,
)

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
PROJECT = "prj_" + ULID
WORKSPACE = "wsp_" + ULID
CHAT = "cht_" + ULID
CRON = "cron_" + ULID
SKILL = "psk_" + ULID
CLEANUP = "cln_" + ULID
PATH_VALUES = {
    "project_id": PROJECT,
    "workspace_id": WORKSPACE,
    "chat_id": CHAT,
    "cron_id": CRON,
    "skill_id": SKILL,
}
ARGUMENTS = {
    "deleteWorkspace": (WORKSPACE,),
    "updateChat": (WORKSPACE, CHAT),
    "deleteChat": (WORKSPACE, CHAT),
    "listChatCrons": (WORKSPACE,),
    "getChatCron": (WORKSPACE, CRON),
    "updateChatCron": (WORKSPACE, CRON),
    "deleteChatCron": (WORKSPACE, CRON),
    "listProjectSkills": (PROJECT,),
    "createProjectSkill": (PROJECT,),
    "getProjectSkill": (PROJECT, SKILL),
    "updateProjectSkill": (PROJECT, SKILL),
    "deleteProjectSkill": (PROJECT, SKILL),
    "getProjectUsage": (PROJECT,),
    "getProjectLogs": (PROJECT,),
}
BODIES = {
    "updateChat": {"body": {"title": "Q3 rollout plan"}},
    "updateChatCron": {"body": {"prompt": "Check the rollout"}},
    "createProjectSkill": {"body": {"name": "incident_response", "content": "# Response\n"}},
    "updateProjectSkill": {"body": {"description": None, "content": "# Response\n"}},
}
GROUP_ALIASES = {
    "deleteWorkspace": ("workspaces", "delete"),
    "updateChat": ("chats", "update"),
    "deleteChat": ("chats", "delete"),
    "listChatCrons": ("chat_crons", "list"),
    "getChatCron": ("chat_crons", "get"),
    "updateChatCron": ("chat_crons", "update"),
    "deleteChatCron": ("chat_crons", "delete"),
    "listProjectSkills": ("project_skills", "list"),
    "createProjectSkill": ("project_skills", "create"),
    "getProjectSkill": ("project_skills", "get"),
    "updateProjectSkill": ("project_skills", "update"),
    "deleteProjectSkill": ("project_skills", "delete"),
    "getProjectUsage": ("project_reports", "usage"),
    "getProjectLogs": ("project_reports", "logs"),
}
EXPECTED_RESULT = {
    "deleteWorkspace": models.CleanupResult,
    "updateChat": models.Chat,
    "deleteChat": models.CleanupResult,
    "listChatCrons": (list, models.ChatCron),
    "getChatCron": models.ChatCron,
    "updateChatCron": models.ChatCron,
    "deleteChatCron": None,
    "listProjectSkills": (list, models.ProjectSkill),
    "createProjectSkill": models.ProjectSkill,
    "getProjectSkill": models.ProjectSkill,
    "updateProjectSkill": models.ProjectSkill,
    "deleteProjectSkill": None,
    "getProjectUsage": models.UsageReportCollection,
    "getProjectLogs": models.ProjectLogCollection,
}
NEW_OPERATIONS = tuple(ARGUMENTS)
MUTATIONS = (
    "deleteWorkspace",
    "updateChat",
    "deleteChat",
    "updateChatCron",
    "deleteChatCron",
    "createProjectSkill",
    "updateProjectSkill",
    "deleteProjectSkill",
)


def _client(async_, handler, retry=None):
    cls = AsyncEnvaiClient if async_ else EnvaiClient
    return cls(
        "project-key",
        base_url="https://example.test/",
        transport=httpx.MockTransport(handler),
        retry=retry or RetryPolicy(max_retries=0, base_delay=0, max_delay=0),
    )


async def _invoke(value):
    return await value if inspect.isawaitable(value) else value


async def _close(client):
    if isinstance(client, AsyncEnvaiClient):
        await client.aclose()
    else:
        client.close()


def _call(client, name, *, via_group=False, **overrides):
    if via_group:
        service, alias = GROUP_ALIASES[name]
        method = getattr(getattr(client, service), alias)
    else:
        method = getattr(client, snake(name))
    kwargs = {**BODIES.get(name, {}), **overrides}
    return method(*ARGUMENTS[name], **kwargs)


def _expected_path(name):
    path = "/api/v1" + OPERATIONS[name]["path"]
    for parameter in OPERATIONS[name]["params"]:
        if parameter["in"] == "path":
            path = path.replace("{" + parameter["name"] + "}", PATH_VALUES[parameter["name"]])
    return path


def _assert_result(name, result):
    expected = EXPECTED_RESULT[name]
    if expected is None:
        assert result is None
    elif isinstance(expected, tuple):
        assert isinstance(result, list)
        assert all(isinstance(item, expected[1]) for item in result)
    else:
        assert isinstance(result, expected)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("via_group", [False, True])
@pytest.mark.parametrize("name", NEW_OPERATIONS)
async def test_project_operations_wire_and_envelopes(async_, via_group, name):
    op = OPERATIONS[name]
    payload = sample(op["response"]) if op["response"] else None
    status = 200 if op["response"] else 204
    seen = []
    statuses = []

    def handler(request):
        seen.append(request)
        statuses.append(status)
        return httpx.Response(status, json=payload, headers={"X-Envai-Contract-Version": "3"})

    client = _client(async_, handler)
    try:
        result = await _invoke(_call(client, name, via_group=via_group))
        assert len(seen) == 1 and statuses == [status]
        request = seen[0]
        assert request.method == op["method"] and request.url.path == _expected_path(name)
        assert request.headers["authorization"] == "Bearer project-key"
        assert request.headers.get("x-envai-project-id") is None
        if op["request"]:
            assert json.loads(request.content) == BODIES[name]["body"]
        else:
            assert not request.content
        if payload is not None:
            assert "data" in payload
            if not op["unwrap"]:
                assert {"data", "links", "meta"} <= set(payload)
        _assert_result(name, result)
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("name", NEW_OPERATIONS)
async def test_project_operations_cancel_before_network(async_, name):
    seen = []
    cancel = threading.Event()
    cancel.set()
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200))
    try:
        with pytest.raises(CancelledError):
            await _invoke(_call(client, name, request_options=RequestOptions(cancel=cancel)))
        assert seen == []
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("name", NEW_OPERATIONS)
async def test_project_operations_payment_error_path(async_, name):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            402,
            json={"error": {"type": "billing_error", "code": "credit_exhausted", "message": "x"}},
        )

    client = _client(async_, handler)
    try:
        with pytest.raises(PaymentRequiredError) as error:
            await _invoke(_call(client, name))
        assert error.value.code == "credit_exhausted" and error.value.http_status == 402
        assert len(seen) == 1
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("status,cls,kind,code", [
    (402, PaymentRequiredError, "billing_error", "credit_exhausted"),
    (503, ServerError, "provider_configuration_error", "provider_configuration_error"),
])
@pytest.mark.parametrize("name", MUTATIONS)
async def test_project_mutations_never_retry_payment_or_provider_errors(
    async_, status, cls, kind, code, name
):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={"error": {"type": kind, "code": code}})

    client = _client(async_, handler, RetryPolicy(max_retries=2, base_delay=0, max_delay=0))
    try:
        with pytest.raises(cls) as error:
            await _invoke(_call(client, name))
        assert error.value.code == code and error.value.http_status == status
        assert len(seen) == 1 and OPERATIONS[name]["method"] != "GET"
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("name", ["deleteWorkspace", "deleteChat"])
async def test_cleanup_deletes_return_pending_200(async_, name):
    resource_type = "workspace" if name == "deleteWorkspace" else "chat"
    payload = {
        "data": {
            "resource_type": resource_type,
            "resource_id": WORKSPACE if resource_type == "workspace" else CHAT,
            "cleanup": [{
                "id": CLEANUP,
                "kind": resource_type,
                "status": "pending",
                "attempts": 0,
                "deadline_at": None,
                "started_at": None,
                "finished_at": None,
                "last_error_code": None,
            }],
        }
    }
    statuses = []

    def handler(request):
        statuses.append(200)
        return httpx.Response(200, json=payload)

    client = _client(async_, handler)
    try:
        result = await _invoke(_call(client, name))
        assert isinstance(result, models.CleanupResult)
        assert result.resource_type == resource_type
        assert result.cleanup[0].status == "pending"
        assert statuses == [200]
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("name", ["deleteChatCron", "deleteProjectSkill"])
async def test_cron_and_skill_deletes_return_204(async_, name):
    statuses = []

    def handler(request):
        statuses.append(204)
        return httpx.Response(204)

    client = _client(async_, handler)
    try:
        assert await _invoke(_call(client, name)) is None
        assert statuses == [204]
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("name", ["deleteWorkspace", "deleteChat"])
async def test_deletion_conflict_never_cancels_cron_work(async_, name):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(409, json={"error": {"type": "conflict", "code": "active_cron"}})

    client = _client(async_, handler, RetryPolicy(max_retries=2, base_delay=0, max_delay=0))
    try:
        with pytest.raises(ConflictError) as error:
            await _invoke(_call(client, name))
        assert error.value.code == "active_cron" and len(seen) == 1
        assert "/crons" not in seen[0].url.path and "/cancel" not in seen[0].url.path
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
async def test_update_chat_accepts_only_title(async_):
    seen = []
    payload = sample(OPERATIONS["updateChat"]["response"])
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200, json=payload))
    try:
        result = await _invoke(_call(client, "updateChat", body={"title": None}))
        assert isinstance(result, models.Chat)
        assert json.loads(seen[0].content) == {"title": None}
        for body in ({"team_id": "team_" + ULID}, {"title": "x", "project_id": PROJECT}):
            with pytest.raises(ValidationError):
                await _invoke(_call(client, "updateChat", body=body))
        assert len(seen) == 1
    finally:
        await _close(client)


@pytest.mark.parametrize("async_", [False, True])
async def test_project_skill_omits_unset_fields_and_preserves_markdown(async_):
    markdown = "\n  # Incident response\n\n  Escalate after triage.  \n"
    payload = sample(OPERATIONS["updateProjectSkill"]["response"])
    payload["data"].update({
        "created_by_user_id": None,
        "description": None,
        "content": markdown,
    })
    seen = []
    client = _client(async_, lambda request: seen.append(request) or httpx.Response(200, json=payload))
    try:
        body = models.ProjectSkillUpdateRequest(description=None, content=markdown)
        result = await _invoke(_call(client, "updateProjectSkill", body=body))
        assert json.loads(seen[0].content) == {"description": None, "content": markdown}
        assert result.content == markdown and result.description is None
        assert result.created_by_user_id is None
        assert result.model_dump(mode="json", exclude_unset=True) == payload["data"]
        await _invoke(_call(client, "updateProjectSkill", body=models.ProjectSkillUpdateRequest()))
        assert json.loads(seen[1].content) == {}
    finally:
        await _close(client)
