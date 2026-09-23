"""Evidence-backed capability and worker identity registry primitives.

The registry is deliberately conservative: declarations are useful metadata but
cannot make a claim scheduler-eligible without observed proof and freshness.
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum, StrEnum
from typing import Any, Iterable


class TrustTier(IntEnum):
    SELF_DECLARED = 10
    OBSERVED = 20
    PROBED = 30
    BENCHMARKED = 40
    CURATOR_VERIFIED = 50


class ResourceKind(StrEnum):
    LINUX_WORKER = "linux_worker"
    ANDROID_WORKER = "android_worker"
    EMBEDDED_DEVICE = "embedded_device"
    INSTRUMENT = "instrument"


@dataclass(frozen=True)
class CapabilityClaim:
    name: str
    value: Any
    trust_tier: TrustTier
    proof_type: str
    evidence_refs: tuple[str, ...]
    last_verified: datetime
    ttl_seconds: int
    revoked: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityClaim":
        stamp = datetime.fromisoformat(str(value["last_verified"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return cls(
            name=str(value["name"]), value=value.get("value"),
            trust_tier=TrustTier(int(value["trust_tier"])), proof_type=str(value["proof_type"]),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs", [])),
            last_verified=stamp.astimezone(timezone.utc), ttl_seconds=max(0, int(value["ttl_seconds"])),
            revoked=bool(value.get("revoked", False)),
        )

    def fresh(self, now: datetime | None = None) -> bool:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return not self.revoked and now <= self.last_verified + timedelta(seconds=self.ttl_seconds)

    def scheduler_eligible(self, now: datetime | None = None) -> bool:
        return self.trust_tier >= TrustTier.OBSERVED and bool(self.evidence_refs) and self.fresh(now)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "trust_tier": int(self.trust_tier),
                "trust_tier_name": self.trust_tier.name.lower(), "proof_type": self.proof_type,
                "evidence_refs": list(self.evidence_refs), "last_verified": self.last_verified.isoformat(),
                "ttl_seconds": self.ttl_seconds, "revoked": self.revoked}


@dataclass(frozen=True)
class WorkerIdentity:
    device_id: str
    agent_id: str
    instance_id: str
    worker_id: str
    branch_namespace: str
    resource_kind: ResourceKind = ResourceKind.LINUX_WORKER

    @classmethod
    def create(cls, device_id: str, agent_id: str, instance_id: str,
               resource_kind: ResourceKind = ResourceKind.LINUX_WORKER) -> "WorkerIdentity":
        parts = tuple(part.strip() for part in (device_id, agent_id, instance_id))
        if any(not part or "/" in part or "\\" in part for part in parts):
            raise ValueError("identity components must be non-empty path-safe aliases")
        worker_id = "wkr-" + hashlib.sha256(":".join(parts).encode()).hexdigest()[:32]
        namespace = f"work/{device_id}/{agent_id}/{instance_id}"
        return cls(device_id=parts[0], agent_id=parts[1], instance_id=parts[2],
                   worker_id=worker_id, branch_namespace=namespace, resource_kind=resource_kind)

    def as_dict(self) -> dict[str, str]:
        return {"device_id": self.device_id, "agent_id": self.agent_id, "instance_id": self.instance_id,
                "worker_id": self.worker_id, "branch_namespace": self.branch_namespace,
                "resource_kind": self.resource_kind.value}


@dataclass(frozen=True)
class RegistryEntry:
    identity: WorkerIdentity
    claims: tuple[CapabilityClaim, ...]
    quarantined: bool = False
    quarantine_reason: str | None = None

    def eligible_claims(self, now: datetime | None = None) -> dict[str, Any]:
        if self.quarantined or self.identity.resource_kind == ResourceKind.INSTRUMENT:
            return {}
        return {claim.name: claim.value for claim in self.claims if claim.scheduler_eligible(now)}

    def scheduler_view(self, now: datetime | None = None) -> dict[str, Any]:
        claims = self.eligible_claims(now)
        return {"worker_id": self.identity.worker_id, "identity": self.identity.as_dict(),
                "capabilities": claims, "eligible": bool(claims) and not self.quarantined,
                "quarantined": self.quarantined, "quarantine_reason": self.quarantine_reason}


def merge_claims(existing: Iterable[CapabilityClaim], incoming: Iterable[CapabilityClaim]) -> tuple[CapabilityClaim, ...]:
    """Merge by claim name, retaining the strongest valid provenance.

    A lower-trust or evidence-free update cannot erase a stronger claim. Revocation
    is authoritative and remains visible so history is not silently lost.
    """
    merged = {claim.name: claim for claim in existing}
    for claim in incoming:
        old = merged.get(claim.name)
        if claim.revoked or old is None or claim.trust_tier > old.trust_tier or (
            claim.trust_tier == old.trust_tier and claim.last_verified >= old.last_verified
        ):
            merged[claim.name] = claim
    return tuple(merged[name] for name in sorted(merged))


def quarantine_duplicates(entries: Iterable[RegistryEntry]) -> tuple[RegistryEntry, ...]:
    """Quarantine duplicate live worker identities, deterministically."""
    records = list(entries)
    by_worker: dict[str, list[int]] = {}
    for index, entry in enumerate(records):
        if not entry.quarantined:
            by_worker.setdefault(entry.identity.worker_id, []).append(index)
    for worker_id, indexes in by_worker.items():
        if len(indexes) > 1:
            for index in indexes:
                records[index] = RegistryEntry(records[index].identity, records[index].claims, True,
                                               f"DUPLICATE_LIVE_WORKER_ID:{worker_id}")
    return tuple(records)


def migrate_node_record(node: dict[str, Any], evidence_ref: str) -> RegistryEntry:
    """Convert legacy NODES.yaml metadata without upgrading its trust."""
    identity = WorkerIdentity.create(str(node["id"]), "legacy", "migration-1",
                                     ResourceKind.LINUX_WORKER if node.get("role") else ResourceKind.INSTRUMENT)
    stamp = datetime.now(timezone.utc)
    claims = [CapabilityClaim(str(name), value, TrustTier.SELF_DECLARED, "legacy_nodes_yaml",
                              (evidence_ref,), stamp, 0) for name, value in (node.get("capabilities") or {}).items()]
    return RegistryEntry(identity, tuple(claims))


def linux_probe() -> dict[str, Any]:
    """Collect bounded, non-secret Linux facts for observed/probed claims."""
    def command(name: str, *args: str) -> str:
        path = shutil.which(name)
        if not path:
            return ""
        try:
            result = subprocess.run([path, *args], capture_output=True, text=True, timeout=3, check=False)
            return (result.stdout or "").strip()[:512] if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return {"os": platform.system().lower(), "architecture": platform.machine(),
            "kernel": platform.release(), "python": platform.python_version(),
            "git": bool(shutil.which("git")), "python3_path": command("python3", "-c", "import sys; print(sys.executable)")}


def capability_fingerprint(claims: Iterable[CapabilityClaim]) -> str:
    payload = [claim.as_dict() for claim in sorted(claims, key=lambda item: item.name)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def new_instance_id() -> str:
    return str(uuid.uuid4())
