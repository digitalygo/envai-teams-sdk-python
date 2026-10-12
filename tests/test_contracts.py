import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from envai._schema import EVENTS, OPENAPI

F = Path(__file__).parent / "fixtures"


def test_exact_source_provenance_and_schema_validity():
    provenance = json.loads((F / "provenance.json").read_text())
    for name, digest in provenance["files"].items():
        assert hashlib.sha256((F / name).read_bytes()).hexdigest() == digest
    package = Path(__file__).parents[1] / "src/envai/_contracts"
    for name in ("openapi.yaml", "events.json"):
        assert (package / name).read_bytes() == (F / name).read_bytes()
    Draft202012Validator.check_schema(EVENTS)
    for schema in OPENAPI["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)


def test_all_contract_refs_resolve_locally():
    def visit(value, root):
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                target = (
                    EVENTS
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
