"""Independent byte pins from the reviewed immutable backend commit."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
PINS = {'events-v2.json': (47787,
                    '88d41835f88c0cc4ad071b06ed4623882c6b64144baf312c4a095504c98cd7d6',
                    '02b13b23cd06eacd81059d1ed99e0027769d75e4'),
 'events.json': (67983,
                 '3e21302b1e2cbef0269aa293d75d6cd8d8cc82b8832a5dbba409530ed876a460',
                 '13f60940fbf107e6ee5b1c6e02b051f9403843e5'),
 'openapi.yaml': (493110,
                  '382c85424131410826df59935948b4a9f1aaf6ee8137bec14ebdce26574eec24',
                  '1d2d7b67677b79e830c05b443f9fb07af0ef65a5'),
 'smidja-native.json': (13825,
                        '035065640fa4946a2e54297b1a22c69ea736827d38765b0cdaa80a28c3058f8b',
                        '6fd7074e23381d8875fc6e0b3aef4df99ab6297d'),
 'terminal-snapshot.json': (8578,
                            'c132b27031595c94a8b05eaf008545ccc3ac21d4dd170dd800c5e15b7f93e9bf',
                            '77d885bda661800972ed56837e69a57ab613da82'),
 'wiki.json': (19558,
               'e360060a42c1d69a6cd15de3ea33745567d23166785efcb08f161c7acb38d30c',
               '3d495bbe3b4cf53af3cab29d86d3a032a7fa8b8c')}


def test_current_contract_sources_are_exact_and_separate_from_legacy():
    manifest = json.loads((ROOT / "tests/fixtures/contract3-provenance.json").read_text())
    assert manifest["repository"] == "digitalygo/envai-teams"
    assert manifest["source_state"] == "immutable_backend_commit"
    assert manifest["commit"] == "964c11fe72887329e8d06c2afb483442d643b290"
    assert manifest["contract_version"] == 3 and manifest["operations"] == 78
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
