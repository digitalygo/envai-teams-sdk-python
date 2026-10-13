"""Independent byte pins from the reviewed immutable backend commit."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
PINS = {
    "openapi.yaml": (
        219604,
        "d6c62be0cc562c1d4ab7c00261b46958a5fcd8fa414cbb99a2d2fc7770a4fd14",
        "7ea7d8d9e4f175ba285ca05227147d2de5ee41a4",
    ),
    "events.json": (
        51786,
        "853704fc39c1538825921c1a55f25e61ce8140e64d95a70a3add2548423be702",
        "514c16c00fe7d489e04f4284d99474da0fd1834b",
    ),
    "events-v2.json": (
        47787,
        "88d41835f88c0cc4ad071b06ed4623882c6b64144baf312c4a095504c98cd7d6",
        "02b13b23cd06eacd81059d1ed99e0027769d75e4",
    ),
    "wiki.json": (
        18820,
        "e7a41ff28309539c47fe19ad882342f2d20fffee7705c3724968ca4889238f6a",
        "d1fcb97738ebf58b95b39e06660c74959a8d290b",
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
    assert manifest["commit"] == "c27963d0848540679bccf30d2aeab354097b1a38"
    assert manifest["contract_version"] == 3 and manifest["operations"] == 65
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
