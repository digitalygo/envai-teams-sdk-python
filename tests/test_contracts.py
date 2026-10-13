import hashlib
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from envai._schema import EVENTS, LEGACY_OPENAPI, OPENAPI, OPERATIONS

F = Path(__file__).parent / "fixtures"


# Independent pins prevent a fixture and its manifest being changed together
# while still claiming to be the exact server source at this immutable commit.
PINNED_SOURCES = {
    "openapi.yaml": (
        "packages/contracts/openapi.yaml",
        173824,
        "a68b1eb9cae0500c95104d97118d0d287b887969b9bc2b9dde62bb1e08a400b1",
        "2d3fe7581229a6fb7a798c99ecccfe5f823fa6ed",
    ),
    "events.json": (
        "packages/contracts/events.json",
        46476,
        "f22869556e488f1e208addf258a25c502966746fcc8e2747c07856d30c28aee7",
        "5ac832ff2498d9c2721909a6f342dda62ec46f8b",
    ),
}


def test_exact_source_provenance_and_schema_validity():
    root = Path(__file__).parents[1]
    provenance = json.loads((F / "provenance.json").read_text())
    assert provenance["repository"] == "digitalygo/envai-teams"
    assert provenance["commit"] == "55275860c9a950eaad8e39e9d2b728149084779c"
    assert provenance["protocol_version"] == 2
    assert provenance["operations"] == 52
    assert set(provenance["sources"]) == set(PINNED_SOURCES)
    for name, (path, size, digest, blob) in PINNED_SOURCES.items():
        source = provenance["sources"][name]
        assert source == {
            "repository_path": path,
            "snapshot_path": "tests/fixtures/" + name,
            "bytes": size,
            "sha256": digest,
            "git_blob_sha": blob,
        }
        raw = (root / source["snapshot_path"]).read_bytes()
        assert len(raw) == size
        assert hashlib.sha256(raw).hexdigest() == digest
        assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == blob
    assert set(provenance["generated_fixtures"]) == {
        "events-invalid.json",
        "events-valid.json",
        "operations.json",
        "signatures.json",
        "sse-cases.json",
    }
    for name, fixture in provenance["generated_fixtures"].items():
        assert fixture["snapshot_path"] == "tests/fixtures/" + name
        assert fixture["origin"]
        assert "repository_path" not in fixture and "git_blob_sha" not in fixture
        assert (
            hashlib.sha256((root / fixture["snapshot_path"]).read_bytes()).hexdigest()
            == (fixture["sha256"])
        )
    package = root / "src/envai/_contracts"
    assert (package / "openapi-v2.yaml").read_bytes() == (F / "openapi.yaml").read_bytes()
    assert json.loads((package / "openapi-v2.json").read_text()) == yaml.safe_load(
        (F / "openapi.yaml").read_text()
    )
    Draft202012Validator.check_schema(EVENTS)
    for schema in OPENAPI["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)


def test_derived_operation_inventory_matches_pinned_source():
    operations = json.loads((F / "operations.json").read_text())
    expected = [
        {
            "id": operation["operationId"],
            "method": method.upper(),
            "path": path,
            "authenticated": bool(operation.get("security", OPENAPI.get("security"))),
        }
        for path, item in LEGACY_OPENAPI["paths"].items()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    ]
    assert len(operations) == 52
    assert operations == expected
    assert {x["id"] for x in operations} <= set(OPERATIONS)
    current = [
        {
            "id": name,
            "method": operation["method"],
            "path": operation["path"],
            "authenticated": operation["auth"],
        }
        for name, operation in OPERATIONS.items()
    ]
    assert len(current) == 65
    assert current == [
        {
            "id": op["operationId"],
            "method": method.upper(),
            "path": path,
            "authenticated": bool(op.get("security", OPENAPI.get("security"))),
        }
        for path, methods in OPENAPI["paths"].items()
        for method, op in methods.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]


def test_all_contract_refs_resolve_locally():
    def visit(value, root):
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                target = (
                    __import__("envai._schema", fromlist=["LEGACY_EVENTS"]).LEGACY_EVENTS
                    if "events-v2.json" in ref
                    else EVENTS
                    if ref.startswith("./events.json") or ref.startswith("events.json")
                    else root
                )
                path = ref.split("#", 1)[1]
                for part in path.lstrip("/").split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                visit(child, root)
        elif isinstance(value, list):
            for child in value:
                visit(child, root)

    visit(OPENAPI, OPENAPI)
    visit(EVENTS, EVENTS)
