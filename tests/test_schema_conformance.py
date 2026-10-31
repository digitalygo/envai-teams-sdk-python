"""Shared SCHEMA-01 controls use the normal public client; validation is default."""
import json
from pathlib import Path

import httpx
import pytest

from envai import AsyncEnvaiClient, EnvaiClient
from envai.errors import ProtocolError, ValidationError

FIXTURE = json.loads((Path(__file__).parent / "fixtures/parity/schema-fixtures.json").read_text())


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("case", list(FIXTURE["response_cases"]))
async def test_shared_schema_response_validation(async_, case):
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("synthetic", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=FIXTURE["response_cases"][case])))
    try:
        if case == "valid":
            value = await client.agents.get(FIXTURE["agent_id"]) if async_ else client.agents.get(FIXTURE["agent_id"])
            assert value.name == "researcher" and value.mode == "primary"
        else:
            with pytest.raises(ProtocolError):
                await client.agents.get(FIXTURE["agent_id"]) if async_ else client.agents.get(FIXTURE["agent_id"])
    finally:
        await client.aclose() if async_ else client.close()


@pytest.mark.parametrize("async_", [False, True])
async def test_shared_schema_mutation_rejected_before_http(async_):
    calls = []
    client = (AsyncEnvaiClient if async_ else EnvaiClient)("synthetic", transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(500)))
    try:
        with pytest.raises(ValidationError):
            await client.agents.create(body=FIXTURE["mutation"]["body"]) if async_ else client.agents.create(body=FIXTURE["mutation"]["body"])
        assert len(calls) == FIXTURE["mutation"]["expected_http_calls"]
    finally:
        await client.aclose() if async_ else client.close()
