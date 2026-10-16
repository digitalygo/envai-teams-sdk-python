"""Independent byte pins from the reviewed immutable backend commit."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
PINS = {'openapi.yaml': (232844,
                  '3347eaad83510eaf97519a28dac9e74063b66f57d174ed527281c932496c3f0d',
                  '7ea3448fe8d80610ae68b674f5ba902c08e46688'),
 'events.json': (52694,
                 '609628f9a9eb59b9169f20ec7f1edf167a27d449d23b9c2697a17f50811db1e9',
                 '5f971112cb99535bff366bcfa36def7f8dfa7e7d'),
 'events-v2.json': (47787,
                    '88d41835f88c0cc4ad071b06ed4623882c6b64144baf312c4a095504c98cd7d6',
                    '02b13b23cd06eacd81059d1ed99e0027769d75e4'),
 'wiki.json': (18844,
               '94d25f2f2d7bfacee9544cd614cb86baaf1d952be1b714badb38b7a98a8e636f',
               '2a2b1af96c99db7d072a3c766426fa493e881cb0'),
 'terminal-snapshot.json': (8578,
                            'c132b27031595c94a8b05eaf008545ccc3ac21d4dd170dd800c5e15b7f93e9bf',
                            '77d885bda661800972ed56837e69a57ab613da82')}


def test_current_contract_sources_are_exact_and_separate_from_legacy():
    manifest = json.loads((ROOT / "tests/fixtures/contract3-provenance.json").read_text())
    assert manifest["repository"] == "digitalygo/envai-teams"
    assert manifest["source_state"] == "immutable_backend_commit"
    assert manifest["commit"] == "96acf00162984af4a86a671f924c53a18960fae6"
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
