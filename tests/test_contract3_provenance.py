"""Independent byte pins from the reviewed immutable backend commit."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
PINS = {
    "openapi.yaml": (
        257539,
        "f8b8e3936643fc8ce7b3f8d47a988149e3063f1afd3587830df30c6f1d4ba09f",
        "43364937f4acc996cbf6a6ea454bf1e4e924bb1b",
    ),
    "events.json": (
        62064,
        "ea5e00c19d5a8cc2d0782530344595943fcd8d9ce572529bbc178d95765d9835",
        "2629e12faf723ac7cd9940f979821b0594c82004",
    ),
    "events-v2.json": (
        47787,
        "88d41835f88c0cc4ad071b06ed4623882c6b64144baf312c4a095504c98cd7d6",
        "02b13b23cd06eacd81059d1ed99e0027769d75e4",
    ),
    "wiki.json": (
        19558,
        "e360060a42c1d69a6cd15de3ea33745567d23166785efcb08f161c7acb38d30c",
        "3d495bbe3b4cf53af3cab29d86d3a032a7fa8b8c",
    ),
    "terminal-snapshot.json": (
        8578,
        "c132b27031595c94a8b05eaf008545ccc3ac21d4dd170dd800c5e15b7f93e9bf",
        "77d885bda661800972ed56837e69a57ab613da82",
    ),
}


def test_current_contract_sources_are_exact_and_separate_from_legacy():
    manifest = json.loads((ROOT / "tests/fixtures/contract3-provenance.json").read_text())
    assert manifest["repository"] == "digitalygo/envai-teams"
    assert manifest["source_state"] == "immutable_backend_commit"
    assert manifest["commit"] == "20dfd5a6c8ec9e75d3754875d4f94520feb3bbf6"
    assert manifest["contract_version"] == 3 and manifest["operations"] == 76
    assert set(manifest["sources"]) == set(PINS)
    for name, (size, digest, blob) in PINS.items():
        raw = (ROOT / "src/envai/_contracts" / name).read_bytes()
        assert manifest["sources"][name] == {
            "repository_path": "packages/contracts/" + name,
            "bytes": size,
            "sha256": digest,
            "git_blob_sha": blob,
        }
        assert len(raw) == size
        assert hashlib.sha256(raw).hexdigest() == digest
        assert hashlib.sha1(b"blob " + str(size).encode() + b"\0" + raw).hexdigest() == blob
        if name.endswith(".json"):
            Draft202012Validator.check_schema(json.loads(raw))


def test_actual_http_fixture_hashes_and_origin():
    fixtures = ROOT / "tests/fixtures/contract3-http"
    manifest = json.loads((fixtures / "provenance.json").read_text())
    assert manifest["backend_commit"] == "c27963d0848540679bccf30d2aeab354097b1a38"
    assert manifest["origin"].startswith("Synthetic authenticated control-plane")
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((fixtures / name).read_bytes()).hexdigest() == digest


def test_derived_snapshots_and_shared_attachment_fixtures_have_distinct_provenance():
    manifest = json.loads((ROOT / "tests/fixtures/contract3-provenance.json").read_text())
    assert set(manifest["derived_artifacts"]) == {"openapi.json", "operations.json"}
    assert set(manifest["shared_fixtures"]) == {
        "failed.json",
        "pending.json",
        "ready.json",
        "recoverable.json",
    }
    for collection in ("derived_artifacts", "shared_fixtures"):
        for name, metadata in manifest[collection].items():
            raw = (ROOT / metadata["snapshot_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == metadata["sha256"]
            assert metadata["origin"]
            if collection == "derived_artifacts":
                assert len(raw) == metadata["bytes"]
                assert metadata["source_repository_path"] == "packages/contracts/openapi.yaml"
            else:
                assert (
                    metadata["repository_path"]
                    == "packages/contracts/fixtures/chat-attachments/" + name
                )
