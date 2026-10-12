"""Signed runner regressions shared with the other language SDKs."""

import base64
import copy
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema.validators import validator_for
from test_tools import setup_async, setup_sync

from envai import AsyncJwksVerifier, ClientToolRegistry, JwksVerifier, parse_event
from envai.tool_verification import signing_payload

CASES = json.loads((Path(__file__).parent / "fixtures/tool-schema-matrix.json").read_text())


@pytest.fixture
def sign_event(tool_event):
    # Fresh local test key only; no service credentials or production endpoints.
    key = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode().rstrip("=")
    jwks = {"keys": [{"kid": "schema-test", "kty": "OKP", "crv": "Ed25519", "x": public}]}

    def signed(args):
        raw = copy.deepcopy(tool_event.raw)
        raw["data"]["args"] = copy.deepcopy(args)
        signature = base64.urlsafe_b64encode(key.sign(signing_payload(raw["data"])))
        raw["data"]["signature"] = {
            "alg": "EdDSA",
            "kid": "schema-test",
            "sig": signature.decode().rstrip("="),
        }
        return parse_event(raw), jwks

    return signed


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
@pytest.mark.parametrize("valid", [True, False], ids=["valid", "invalid"])
@pytest.mark.parametrize("async_", [False, True], ids=["sync", "async"])
async def test_schema_matrix_through_signed_runner(case, valid, async_, sign_event):
    schema = case["schema"]
    args = case["good" if valid else "bad"]
    reference = validator_for(schema)
    reference.check_schema(schema)
    assert reference(schema).is_valid(args) is valid
    event, jwks = sign_event(args)
    called = []

    def handler(args, context):
        called.append(copy.deepcopy(args))
        return "ok"

    async def async_handler(args, context):
        return handler(args, context)

    async def fetch():
        return jwks

    if async_:
        client, runner, state, sent = await setup_async(event, async_handler, schema=schema)
        runner.verifier = AsyncJwksVerifier(fetch)
        try:
            await runner.consume(event)
            await runner.wait()
            await runner.consume(event)
            await runner.wait()
        finally:
            await runner.aclose()
            await client.aclose()
    else:
        client, runner, state, sent = setup_sync(event, handler, schema=schema)
        runner.verifier = JwksVerifier(lambda: jwks)
        try:
            runner.consume(event)
            runner.wait()
            runner.consume(event)
            runner.wait()
        finally:
            runner.close()
            client.close()
    assert called == ([args] if valid else [])
    assert len(sent) == 1
    body = json.loads(sent[0].content)
    assert body["is_error"] is not valid
    assert body["output"] == ("ok" if valid else '{"error":"invalid_tool_arguments"}')


@pytest.mark.parametrize("keyword", ["const", "enum", "default", "examples"])
def test_reference_like_literal_data_is_not_a_schema(keyword):
    literal = {"$ref": "https://example.invalid/data", "$schema": "ordinary-data", "items": []}
    value = [literal] if keyword in {"enum", "examples"} else literal
    schema = {"type": "object", keyword: value}
    registry = ClientToolRegistry()
    registry.register("literal", lambda args, ctx: None, schema)
    assert registry.get("literal").schema == schema


def test_reference_named_property_is_not_itself_a_reference():
    registry = ClientToolRegistry()
    registry.register(
        "literal",
        lambda args, ctx: None,
        {"type": "object", "properties": {"$ref": {"type": "string"}}},
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "$schema": "https://example.invalid/schema"},
        {"type": "object", "properties": {"x": {"$schema": "https://example.invalid/schema"}}},
    ],
)
def test_unsupported_dialects_are_rejected(schema):
    with pytest.raises(ValueError, match="Unsupported JSON Schema dialect"):
        ClientToolRegistry().register("unsupported", lambda args, ctx: None, schema)


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_external_references_in_schema_locations_remain_blocked(keyword):
    with pytest.raises(ValueError, match="External schema references"):
        ClientToolRegistry().register(
            "remote",
            lambda args, ctx: None,
            {
                "type": "object",
                "properties": {"nested": {keyword: "https://example.invalid/schema"}},
            },
        )


def test_draft7_schema_dependency_remote_reference_remains_blocked():
    with pytest.raises(ValueError, match="External schema references"):
        ClientToolRegistry().register(
            "remote",
            lambda args, ctx: None,
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "dependencies": {"x": {"$ref": "https://example.invalid/schema"}},
            },
        )
