from datetime import datetime, timedelta, timezone

from hermes_fleet.capability_registry import (
    CapabilityClaim,
    RegistryEntry,
    ResourceKind,
    TrustTier,
    WorkerIdentity,
    capability_fingerprint,
    merge_claims,
    quarantine_duplicates,
)
from hermes_fleet.routing import route


NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def claim(name, value, tier=TrustTier.PROBED, evidence=("probe:linux",), ttl=3600, when=NOW, revoked=False):
    return CapabilityClaim(name, value, tier, "runtime_probe", evidence, when, ttl, revoked)


def test_identity_namespaces_are_distinct_for_simultaneous_agents():
    left = WorkerIdentity.create("gpd-win-mini", "hermes", "instance-1")
    right = WorkerIdentity.create("gpd-win-mini", "hermes", "instance-2")
    assert left.worker_id != right.worker_id
    assert left.branch_namespace != right.branch_namespace


def test_claim_merge_cannot_upgrade_self_declaration_or_erase_provenance():
    existing = claim("gpu", True, TrustTier.BENCHMARKED, ("bench:42",))
    weaker = claim("gpu", False, TrustTier.SELF_DECLARED, ())
    merged = merge_claims([existing], [weaker])
    assert merged[0] == existing
    assert capability_fingerprint(merged)


def test_expired_revoked_and_unproven_claims_are_ineligible():
    entry = RegistryEntry(
        WorkerIdentity.create("node", "agent", "instance"),
        (claim("git", True), claim("old", True, when=NOW - timedelta(seconds=10), ttl=1),
         claim("revoked", True, revoked=True), claim("declared", True, TrustTier.SELF_DECLARED, ())),
    )
    assert entry.eligible_claims(NOW) == {"git": True}


def test_duplicate_live_identities_are_quarantined_and_instruments_never_route():
    identity = WorkerIdentity.create("node", "agent", "same")
    entries = quarantine_duplicates((RegistryEntry(identity, (claim("git", True),)), RegistryEntry(identity, (claim("git", True),))))
    assert all(item.quarantined for item in entries)
    instrument = RegistryEntry(WorkerIdentity.create("scope", "sensor", "one", ResourceKind.INSTRUMENT), (claim("rf", True),))
    assert instrument.scheduler_view(NOW)["eligible"] is False


def test_scheduler_uses_only_registry_projection():
    node = {"id": "n1", "status": "online", "last_seen": "2999-01-01T00:00:00+00:00",
            "capabilities": {"os": "linux", "profiles": ["engineer"], "tools": ["git"]},
            "registry": {"eligible": False, "quarantined": False, "capabilities": {"os": "linux", "profiles": ["engineer"], "tools": ["git"]}}}
    selected, explanation = route([node], {"tools": ["git"]}, "engineer", 1)
    assert selected is None
    assert explanation["candidates"][0]["reason_codes"] == ["REGISTRY_INELIGIBLE"]
