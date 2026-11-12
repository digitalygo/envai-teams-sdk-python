"""Synthetic project-key responses and authentication-boundary regressions."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from envai import AsyncEnvaiClient, EnvaiClient, models
from envai._schema import OPENAPI, OPERATIONS
from envai.definitions.service import normalize
from envai.errors import ProtocolError, UnsupportedOperationError

FIXTURES = json.loads((Path(__file__).parent / "fixtures/project-tenancy.json").read_text())


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("operation", FIXTURES)
@pytest.mark.parametrize("creator", [None, "usr_01ARZ3NDEKTSV4RRFFQ69G5FAV"])
async def test_project_key_resource_hydration(async_, operation, creator):
    from test_http import snake

    payload = copy.deepcopy(FIXTURES[operation])
    row = payload["data"]
    owner = "user_id" if operation == "getCompletion" else "created_by_user_id"
    row[owner] = creator
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload, headers={"X-Envai-Contract-Version": "3"})

    client = (AsyncEnvaiClient if async_ else EnvaiClient)("project-key", transport=httpx.MockTransport(handler))
    args = {p["name"]: row["id"] if p["name"] in ("agent_id", "completion_id") else row.get(p["name"], row["id"])
            for p in OPERATIONS[operation]["params"] if p["in"] == "path"}
    try:
        result = getattr(client, snake(operation))(**args)
        result = await result if async_ else result
        assert result.project_id == row["project_id"] and getattr(result, owner) == creator
        if operation == "getCompletion":
            assert result.api_key_id == row["api_key_id"]
        assert result.model_dump(mode="json", exclude_unset=True) == row
        assert len(seen) == 1 and seen[0].headers["Authorization"] == "Bearer project-key"
        del row["project_id"]
        with pytest.raises(ProtocolError):
            result = getattr(client, snake(operation))(**args)
            if async_:
                await result
    finally:
        await client.aclose() if async_ else client.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_api_key_management_fails_before_network(async_):
    def handler(request):
        pytest.fail("Session-only operation reached network")

    client = (AsyncEnvaiClient if async_ else EnvaiClient)("project-key", transport=httpx.MockTransport(handler))
    try:
        for method, args in [(client.api_keys.list, {}), (client.api_keys.create, {"body": {"name": "test"}}),
                             (client.api_keys.delete, {"key_id": "key_test"})]:
            with pytest.raises(UnsupportedOperationError) as exc:
                result = method(**args)
                if async_:
                    await result
            assert exc.value.code == "session_required" and "human session and CSRF" in exc.value.message
    finally:
        await client.aclose() if async_ else client.close()


def test_definitions_ignore_server_ownership_metadata():
    desired = {"name": "researcher", "mode": "primary"}
    assert normalize("agent", {**desired, "id": "agt_x", "project_id": "prj_x", "created_by_user_id": None}, {}) == normalize("agent", desired, {})


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("verb", ["create", "list", "get", "update", "delete"])
async def test_deprecated_tool_aliases_use_project_operations(async_, verb):
    from test_http import arguments, sample

    operation = verb + ("ProjectTools" if verb == "list" else "ProjectTool")
    op = OPERATIONS[operation]
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200 if op["response"] else 204,
                              json=sample(op["response"]) if op["response"] else None)

    client = (AsyncEnvaiClient if async_ else EnvaiClient)("project-key", transport=httpx.MockTransport(handler))
    try:
        result = getattr(client, verb + ("_user_tools" if verb == "list" else "_user_tool"))(**arguments(op))
        if async_:
            await result
        assert len(seen) == 1 and seen[0].url.path.startswith("/api/v1/tools")
        assert seen[0].method == op["method"]
    finally:
        await client.aclose() if async_ else client.close()


def test_only_reviewed_operations_have_client_methods():
    from test_http import snake

    from envai import _generated as generated

    allowed = set(json.loads((Path(__file__).parents[1] / "scripts/sdk-operations.json").read_text()))
    source = {op["operationId"] for item in OPENAPI["paths"].values()
              for op in item.values() if isinstance(op, dict) and "operationId" in op}
    assert set(OPERATIONS) == allowed and len(source) == 146 and len(allowed) == 92
    reviewed_additions = {
        "deleteWorkspace", "updateChat", "deleteChat", "listChatCrons",
        "getChatCron", "updateChatCron", "deleteChatCron", "listProjectSkills",
        "createProjectSkill", "getProjectSkill", "updateProjectSkill",
        "deleteProjectSkill", "getProjectUsage", "getProjectLogs",
    }
    assert len(reviewed_additions) == 14 and reviewed_additions <= allowed
    catalog = models.ProjectEventTypeFiltersResource.model_validate(
        {"data": {"types": ["chat.created", "completion.finished"]}}
    )
    assert catalog.model_dump() == {"data": {"types": ["chat.created", "completion.finished"]}}
    assert "getProjectLogs" in source and "getProjectLogs" in allowed
    assert "getProjectLogs" in OPERATIONS
    assert hasattr(EnvaiClient, "get_project_logs")
    assert hasattr(AsyncEnvaiClient, "get_project_logs")
    session_stubs = {"createApiKey", "listApiKeys", "deleteApiKey"}
    assert session_stubs <= allowed
    human_operations = {
        "login", "createCompany", "getCompanyUsage", "getCompanyBillingWallet",
        "listCompanyBillingTransactions", "createCompanyBillingTopup",
        "setupCompanyBillingPaymentMethod", "updateCompanyAutoRecharge",
        "getProjectSpendLimit", "updateProjectSpendLimit",
        "getApiKeySpendLimit", "updateApiKeySpendLimit", "deleteProject",
        "listUsers", "updateUserPlatformRole", "reserveBilling",
        "settleBilling", "releaseBilling", "internalWikiDispatch",
    }
    assert human_operations <= source and not human_operations & allowed
    human_only = {
        op["operationId"] for item in OPENAPI["paths"].values()
        for op in item.values()
        if isinstance(op, dict) and "operationId" in op and op.get("security")
        and not any("bearerAuth" in scheme for scheme in op["security"])
    }
    assert session_stubs <= human_only
    assert human_only - session_stubs <= source - allowed
    for operation in human_operations | (source - allowed):
        assert operation not in OPERATIONS
        assert not hasattr(EnvaiClient, snake(operation))
        assert not hasattr(AsyncEnvaiClient, snake(operation))
    assert "createChatCron" not in source and "createChatCron" not in allowed
    assert not hasattr(EnvaiClient, "create_chat_cron")
    assert not hasattr(AsyncEnvaiClient, "create_chat_cron")
    assert not hasattr(generated.ChatCronsService, "create")
    assert not hasattr(generated.AsyncChatCronsService, "create")
    assert not any(
        method == "post" and "crons" in path
        for path, item in OPENAPI["paths"].items() for method in item
    )


@pytest.mark.parametrize("metadata", [{"environment": "staging"}, [], ["one", 2]])
def test_workspace_metadata_object_or_list_roundtrip(metadata):
    payload = copy.deepcopy(FIXTURES["getWorkspace"])
    payload["data"]["metadata"] = metadata
    with EnvaiClient("project-key", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        result = client.workspaces.get(payload["data"]["id"])
        assert result.model_dump(mode="json")["metadata"] == metadata
        payload["data"]["metadata"] = "invalid scalar"
        with pytest.raises(ProtocolError):
            client.workspaces.get(payload["data"]["id"])


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("listing", [False, True])
async def test_legacy_completion_get_and_page_keep_frozen_shape(async_, listing):
    payload = json.loads((Path(__file__).parent / "fixtures/contract3-http/legacy-completion.json").read_text())
    row = payload["data"]
    if listing:
        payload = {"data": [row], "links": {}, "meta": {"current_page": 1, "total": 1}}
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("project-key", contract_version=2,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload, headers={"X-Envai-Contract-Version": "2"})))
    try:
        result = client.completions.list(row["workspace_id"], row["chat_id"]) if listing else client.completions.get(row["workspace_id"], row["chat_id"], row["id"])
        result = await result if async_ else result
        assert isinstance(result, models.LegacyCompletionCollection if listing else models.LegacyCompletion)
        assert result.model_dump(mode="json", exclude_unset=True) == (payload if listing else row)
        value = result.data[0] if listing else result
        assert value.user_id == row["user_id"] and "project_id" not in type(value).model_fields
    finally:
        await client.aclose() if async_ else client.close()
