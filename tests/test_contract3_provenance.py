"""Independent byte pins from the reviewed immutable backend commit."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
PINS = {
    "openapi.yaml": (
        234213,
        "8d578d43b95685351963fa3fafb08e8a6ac6a346e576188ea3884414b5ff55cd",
        "e56f31c2fb983de6e92ef3c9b4e3b99d3af938ae",
    ),
    "events.json": (
        52694,
        "609628f9a9eb59b9169f20ec7f1edf167a27d449d23b9c2697a17f50811db1e9",
        "5f971112cb99535bff366bcfa36def7f8dfa7e7d",
    ),
    "events-v2.json": (
        47787,
        "88d41835f88c0cc4ad071b06ed4623882c6b64144baf312c4a095504c98cd7d6",
        "02b13b23cd06eacd81059d1ed99e0027769d75e4",
    ),
    "wiki.json": (
        18872,
        "57e08c6058d4e3e532f9c24e0973cbf5f181f153ec764891bc3d88dd6c8004c9",
        "e62b9a866951a6bb56350f811f8ae36ca5ea2377",
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
    assert manifest["commit"] == "7192fc05ebec817bddc705186c8f195cc0dc5ec5"
    assert manifest["contract_version"] == 3 and manifest["operations"] == 71
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
        "failed.json", "pending.json", "ready.json", "recoverable.json"
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
                assert metadata["repository_path"] == "packages/contracts/fixtures/chat-attachments/" + name
