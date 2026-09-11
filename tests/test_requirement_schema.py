"""Contract tests for the versioned requirements schema and its parsers.

The schema is the durable, language-independent definition of the requirement
contract; ``TaskRequirements.from_dict`` is the Python projection of it. These
tests keep the two in step so a job spec cannot silently drift from the schema.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_fleet.requirements import SCHEMA_ID, TaskRequirements

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "execution"
    / "job-requirements.v1.schema.json"
)

EXAMPLE = {
    "schema": "fr3k.job.requirements.v1",
    "job_id": "0199f3d7-example",
    "work_key": "sha256:deadbeef",
    "objective": "Analyze an unknown LoRa profile from capture bundle X",
    "priority": "P1",
    "requires": {
        "os": ["linux"],
        "architectures": ["x86_64", "aarch64"],
        "min_ram_mb": 4096,
        "capabilities": [
            {"id": "compute.python", "verification": "probe_verified"},
            {"id": "blackwave.decode.offline", "verification": "curator_verified"},
        ],
        "locality_required": True,
        "data_refs": ["object://capture/sha256/abc"],
        "trust_level_min": 3,
        "execution_class": "AUTO",
    },
    "prefers": {"plugged_in": True, "low_latency_to_data": True, "accelerators": ["gpu"]},
    "concurrency": {"group": "rf-analysis", "safe": True, "max_parallelism": 2, "exclusive": False},
    "side_effects": {"external_write": False, "physical": False},
}


def test_schema_is_valid_json_with_expected_contract():
    schema = json.loads(SCHEMA_PATH.read_text())
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["schema"]["const"] == SCHEMA_ID
    for required in ("requires", "prefers", "concurrency", "side_effects"):
        assert required in schema["properties"]
    assert "node_snapshot" in schema["$defs"]
    assert set(schema["$defs"]["verification"]["enum"]) == {
        "self_declared",
        "observed",
        "probe_verified",
        "benchmark_verified",
        "curator_verified",
    }


def test_example_spec_parses_into_the_contract():
    raw = EXAMPLE["requires"] | {
        "preferences": EXAMPLE["prefers"],
        "concurrency": EXAMPLE["concurrency"],
        "external_write": EXAMPLE["side_effects"]["external_write"],
        "physical": EXAMPLE["side_effects"]["physical"],
    }
    parsed = TaskRequirements.from_dict(raw)
    assert parsed.schema == SCHEMA_ID
    assert parsed.capabilities[0].id == "compute.python"
    assert parsed.capabilities[1].verification == "curator_verified"
    assert parsed.locality_required is True
    assert parsed.concurrency.group == "rf-analysis"
    assert parsed.concurrency.safe is True
    assert parsed.concurrency.max_parallelism == 2
    assert parsed.preferences.accelerators == ("gpu",)


def test_defaults_match_the_schema_defaults():
    # A bare job spec must inherit the serialized-by-default concurrency policy.
    parsed = TaskRequirements.from_dict({})
    assert parsed.concurrency.safe is False
    assert parsed.concurrency.group == "global"
    assert parsed.concurrency.max_parallelism == 1
    assert parsed.concurrency.exclusive is False
    assert parsed.requires_compute is True
    assert parsed.trust_level_min == 3


def test_unknown_verification_tier_is_rejected():
    import pytest

    from hermes_fleet.requirements import CapabilityNeed

    with pytest.raises(ValueError):
        CapabilityNeed(id="compute.python", verification="definitely_verified")
