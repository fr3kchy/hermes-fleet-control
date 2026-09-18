from hermes_fleet.routing import route


def test_routing_applies_strict_gates_before_scoring():
    nodes = [{"id": "n1", "status": "online", "last_seen": "2999-01-01T00:00:00+00:00", "capabilities": {"os": "linux", "architecture": "x86_64", "profiles": ["engineer"], "tools": ["git"], "gpu": True, "vram_gb": 12}, "policy": {"trust_zone": "trusted", "data_classification": "confidential", "max_risk": 3, "max_concurrency": 2}, "metrics": {}}]
    selected, explanation = route(nodes, {"os": "linux", "tools": ["git"], "gpu": True, "min_vram_gb": 8, "data_classification": "internal"}, "engineer", 2)
    assert selected == "n1"
    assert explanation["candidates"][0]["accepted"] is True
    selected, explanation = route(nodes, {"skills": ["missing"], "data_classification": "internal"}, "engineer", 2)
    assert selected is None
    assert "MISSING_SKILL" in explanation["candidates"][0]["reason_codes"]
