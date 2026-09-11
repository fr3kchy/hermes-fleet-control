"""Versioned, machine-readable task requirements and deterministic placement.

This module implements the capability-aware requirement contract and the
two-stage placement algorithm described in FR3K
``scopes/distributed-execution-fabric/README.md`` section 7:

    eligible_nodes = HARD_FILTER(job.requires, trust, policy, fresh capabilities,
                                 physical connectivity, credentials, locality)
    node           = argmax(soft_score(eligible_nodes))

Design rules enforced here:

* **Hard eligibility precedes soft scoring.** A node that fails a single hard
  constraint can never be returned, no matter how attractive its soft score.
  This is the property that stops a fast-but-unqualified worker from winning
  placement.
* **Deterministic placement.** The same ``(requirements, registry snapshot, now)``
  triple always produces the same candidate list and the same selection. Soft
  scores are computed with integer arithmetic only (no floating point): ties are
  broken by ascending ``node_id``.
* **Evidence retention.** Every placement decision keeps the full candidate list
  with per-node rejection reasons and per-component score breakdowns, so an
  operator can answer *why this node and not that one*.
* **Instrument-only devices are never generic compute workers.** A device that
  only exposes measurement resources cannot be scheduled as a general worker.

The module is pure/logic-only; it consumes a capability-registry *projection*
(a list of :class:`NodeSnapshot`) and never mutates registry state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_ID = "fr3k.job.requirements.v1"

# Capability verification tiers, weakest -> strongest. A requirement asking for
# ``probe_verified`` is satisfied by ``probe_verified``, ``benchmark_verified``
# or ``curator_verified`` but NOT by ``self_declared`` or ``observed``.
VERIFICATION_ORDER: tuple[str, ...] = (
    "self_declared",
    "observed",
    "probe_verified",
    "benchmark_verified",
    "curator_verified",
)

EXECUTION_CLASSES: tuple[str, ...] = ("AUTO", "OBSERVE", "APPROVAL", "PHYSICAL")
SERIALIZED_EXECUTION_CLASSES: tuple[str, ...] = ("APPROVAL", "PHYSICAL")

# Device classes that are measurement/observer endpoints, not generic compute
# workers. They may expose capabilities but must never be handed generic work.
INSTRUMENT_DEVICE_CLASSES: tuple[str, ...] = (
    "instrument",
    "sensor",
    "esp32",
    "tdeck",
    "microcontroller",
)

TRUST_TIERS: Mapping[str, int] = {
    "T0_UNKNOWN": 0,
    "T1_OBSERVER": 1,
    "T2_VERIFIED_SENSOR": 2,
    "T3_TRUSTED_WORKER": 3,
    "T4_PRIVILEGED_EXECUTOR": 4,
    "T5_CONTROL_PLANE": 5,
}

# Safety floors that are HARD (a node below them is ineligible, not merely
# deprioritised) while values above them modulate the soft score.
CRITICAL_BATTERY_PCT = 15

# Soft-scoring weights. Integers keep scoring exactly reproducible.
WEIGHTS: Mapping[str, int] = {
    "reliability": 3,
    "locality": 6,
    "power": 3,
    "energy": 3,
    "thermal": 2,
    "resource": 1,
    "latency": 2,
    "queue": 6,
    "retry": 5,
}


# --------------------------------------------------------------------- model --


@dataclass(frozen=True)
class CapabilityNeed:
    """A capability the job requires, with the minimum accepted verification."""

    id: str
    verification: str = "probe_verified"

    def __post_init__(self) -> None:
        if self.verification not in VERIFICATION_ORDER:
            raise ValueError(
                f"unknown verification tier {self.verification!r}; "
                f"expected one of {VERIFICATION_ORDER}"
            )


@dataclass(frozen=True)
class ConcurrencySpec:
    """Concurrency constraints declared by a job.

    ``safe`` defaults to ``False``: parallel placement is opt-in and is never
    inferred from the mere presence of multiple connected workers.
    """

    group: str = "global"
    safe: bool = False
    max_parallelism: int = 1
    exclusive: bool = False

    def __post_init__(self) -> None:
        if self.max_parallelism < 1:
            raise ValueError("max_parallelism must be >= 1")


@dataclass(frozen=True)
class Preferences:
    """Soft placement preferences (never eligibility criteria)."""

    plugged_in: bool = True
    low_latency_to_data: bool = True
    accelerators: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskRequirements:
    """The versioned requirements contract a scheduler consumes."""

    os: tuple[str, ...] = ()
    architectures: tuple[str, ...] = ()
    min_ram_mb: int = 0
    min_storage_mb: int = 0
    capabilities: tuple[CapabilityNeed, ...] = ()
    credentials: tuple[str, ...] = ()
    data_refs: tuple[str, ...] = ()
    locality_required: bool = False
    trust_level_min: int = 3
    execution_class: str = "AUTO"
    peripherals: tuple[str, ...] = ()
    accelerators: tuple[str, ...] = ()
    transports: tuple[str, ...] = ()
    max_latency_ms: int | None = None
    min_battery_pct: int = 0
    requires_compute: bool = True
    allow_instrument_worker: bool = False
    external_write: bool = False
    physical: bool = False
    preferences: Preferences = field(default_factory=Preferences)
    concurrency: ConcurrencySpec = field(default_factory=ConcurrencySpec)
    schema: str = SCHEMA_ID

    def __post_init__(self) -> None:
        if self.execution_class not in EXECUTION_CLASSES:
            raise ValueError(f"unknown execution_class {self.execution_class!r}")
        if not 0 <= self.trust_level_min <= 5:
            raise ValueError("trust_level_min must be within 0..5")

    # -- (de)serialisation ---------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskRequirements":
        prefs = raw.get("preferences") or {}
        conc = raw.get("concurrency") or {}
        caps = tuple(
            CapabilityNeed(id=str(c["id"]), verification=str(c.get("verification", "probe_verified")))
            if isinstance(c, Mapping)
            else CapabilityNeed(id=str(c))
            for c in raw.get("capabilities", ())
        )
        return cls(
            os=tuple(raw.get("os", ()) or ()),
            architectures=tuple(raw.get("architectures", ()) or ()),
            min_ram_mb=int(raw.get("min_ram_mb", 0) or 0),
            min_storage_mb=int(raw.get("min_storage_mb", 0) or 0),
            capabilities=caps,
            credentials=tuple(raw.get("credentials", ()) or ()),
            data_refs=tuple(raw.get("data_refs", ()) or ()),
            locality_required=bool(raw.get("locality_required", False)),
            trust_level_min=int(raw.get("trust_level_min", 3)),
            execution_class=str(raw.get("execution_class", "AUTO")),
            peripherals=tuple(raw.get("peripherals", ()) or ()),
            accelerators=tuple(raw.get("accelerators", ()) or ()),
            transports=tuple(raw.get("transports", ()) or ()),
            max_latency_ms=(int(raw["max_latency_ms"]) if raw.get("max_latency_ms") is not None else None),
            min_battery_pct=int(raw.get("min_battery_pct", 0) or 0),
            requires_compute=bool(raw.get("requires_compute", True)),
            allow_instrument_worker=bool(raw.get("allow_instrument_worker", False)),
            external_write=bool(raw.get("external_write", False)),
            physical=bool(raw.get("physical", False)),
            preferences=Preferences(
                plugged_in=bool(prefs.get("plugged_in", True)),
                low_latency_to_data=bool(prefs.get("low_latency_to_data", True)),
                accelerators=tuple(prefs.get("accelerators", ()) or ()),
            ),
            concurrency=ConcurrencySpec(
                group=str(conc.get("group", "global")),
                safe=bool(conc.get("safe", False)),
                max_parallelism=int(conc.get("max_parallelism", 1)),
                exclusive=bool(conc.get("exclusive", False)),
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NodeCapability:
    """A capability as *projected* by the live capability registry."""

    id: str
    verification: str = "self_declared"
    expires_at: str | None = None
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if self.verification not in VERIFICATION_ORDER:
            raise ValueError(f"unknown verification tier {self.verification!r}")

    def fresh(self, now: datetime) -> bool:
        if self.expires_at is None:
            return True
        return datetime.fromisoformat(self.expires_at) > now


@dataclass(frozen=True)
class NodeSnapshot:
    """A single point-in-time registry projection of one node.

    This is intentionally a *snapshot*: the scheduler reasons about observed
    state (power, thermal, load, freshness) rather than asking a node to
    describe itself optimistically at claim time.
    """

    node_id: str
    device_class: str
    os: str
    architecture: str
    trust_tier: int
    capabilities: Mapping[str, NodeCapability] = field(default_factory=dict)
    allowed_execution_classes: tuple[str, ...] = ("AUTO", "OBSERVE")
    ram_free_mb: int = 0
    storage_free_mb: int = 0
    accelerators: tuple[str, ...] = ()
    peripherals: tuple[str, ...] = ()
    transports: tuple[str, ...] = ()
    latency_ms: int = 0
    power_source: str = "ac"  # "ac" | "battery"
    battery_pct: int | None = None
    charging: bool = True
    thermal_c: float = 0.0
    thermal_limit_c: float = 95.0
    throttled: bool = False
    data_refs: tuple[str, ...] = ()
    online: bool = True
    quarantined: bool = False
    identity_verified: bool = True
    workspace_id: str | None = None
    active_work: int = 0
    recent_success_rate: int = 100  # percent, 0..100
    recent_failures: int = 0

    @property
    def instrument_only(self) -> bool:
        """True when the node may measure but must not run generic work."""
        return self.device_class in INSTRUMENT_DEVICE_CLASSES

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["capabilities"] = {
            k: asdict(v) for k, v in self.capabilities.items()
        }
        return d


# ---------------------------------------------------------------- filtering --


@dataclass(frozen=True)
class CandidateEvaluation:
    """The scheduler's recorded verdict for one candidate node."""

    node_id: str
    eligible: bool
    score: int | None
    rejections: tuple[str, ...] = ()
    score_components: Mapping[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "eligible": self.eligible,
            "score": self.score,
            "rejections": list(self.rejections),
            "score_components": dict(self.score_components),
        }


def _satisfies_verification(have: str, need: str) -> bool:
    return VERIFICATION_ORDER.index(have) >= VERIFICATION_ORDER.index(need)


def hard_filter(
    req: TaskRequirements,
    node: NodeSnapshot,
    *,
    now: datetime | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Return ``(eligible, rejection_reasons)`` for a single node.

    Every failing constraint contributes a distinct, human-readable reason so
    placement evidence explains *why* an alternative was rejected.
    """
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []

    if not node.online:
        reasons.append("node offline")
    if node.quarantined:
        reasons.append("node quarantined (untrusted/revoked identity)")
    if not node.identity_verified:
        reasons.append("worker identity not verified")

    if node.trust_tier < req.trust_level_min:
        reasons.append(
            f"trust tier T{node.trust_tier} < required T{req.trust_level_min}"
        )
    if req.execution_class not in node.allowed_execution_classes:
        reasons.append(
            f"execution class {req.execution_class} not permitted on this node"
        )

    if req.requires_compute and node.instrument_only and not req.allow_instrument_worker:
        reasons.append(
            "instrument-only device cannot be scheduled as a generic compute worker"
        )

    if req.os and node.os not in req.os:
        reasons.append(f"os {node.os!r} not in {list(req.os)}")
    if req.architectures and node.architecture not in req.architectures:
        reasons.append(f"architecture {node.architecture!r} not in {list(req.architectures)}")

    if node.ram_free_mb < req.min_ram_mb:
        reasons.append(f"free RAM {node.ram_free_mb}MB < required {req.min_ram_mb}MB")
    if node.storage_free_mb < req.min_storage_mb:
        reasons.append(
            f"free storage {node.storage_free_mb}MB < required {req.min_storage_mb}MB"
        )

    for need in req.capabilities:
        cap = node.capabilities.get(need.id)
        if cap is None:
            reasons.append(f"capability {need.id!r} absent")
            continue
        if not _satisfies_verification(cap.verification, need.verification):
            reasons.append(
                f"capability {need.id!r} verification {cap.verification!r} "
                f"< required {need.verification!r}"
            )
        elif not cap.fresh(now):
            reasons.append(f"capability {need.id!r} evidence expired")

    for accel in req.accelerators:
        if accel not in node.accelerators:
            reasons.append(f"accelerator {accel!r} unavailable")
    for periph in req.peripherals:
        if periph not in node.peripherals:
            reasons.append(f"peripheral {periph!r} unavailable")
    if req.transports and not set(req.transports) & set(node.transports):
        reasons.append(
            f"no shared transport: need one of {list(req.transports)}, "
            f"node has {list(node.transports)}"
        )

    if req.max_latency_ms is not None and node.latency_ms > req.max_latency_ms:
        reasons.append(
            f"network latency {node.latency_ms}ms > allowed {req.max_latency_ms}ms"
        )

    if req.locality_required:
        missing = [ref for ref in req.data_refs if ref not in node.data_refs]
        if missing:
            reasons.append(f"required local data absent: {missing}")

    # Hard safety floors: a worker that is about to die or is already thermally
    # saturated must never be selected, regardless of its soft score.
    if node.power_source == "battery" and not node.charging:
        if node.battery_pct is not None and node.battery_pct < CRITICAL_BATTERY_PCT:
            reasons.append(
                f"critical battery {node.battery_pct}% and not charging"
            )
    if node.battery_pct is not None and node.battery_pct < req.min_battery_pct:
        reasons.append(
            f"battery {node.battery_pct}% < required minimum {req.min_battery_pct}%"
        )
    if node.thermal_c >= node.thermal_limit_c:
        reasons.append(
            f"thermal limit reached ({node.thermal_c}C >= {node.thermal_limit_c}C)"
        )

    return (not reasons, tuple(reasons))


# ---------------------------------------------------------------- scoring ----


def _locality_score(req: TaskRequirements, node: NodeSnapshot) -> int:
    if not req.data_refs:
        return 50 if req.preferences.low_latency_to_data else 0
    local = sum(1 for ref in req.data_refs if ref in node.data_refs)
    return int(round(100 * local / len(req.data_refs)))


def _power_score(node: NodeSnapshot) -> int:
    if node.power_source == "ac":
        return 100
    if node.battery_pct is None:
        return 50
    return max(0, min(100, node.battery_pct))


def _thermal_score(node: NodeSnapshot) -> int:
    if node.thermal_limit_c <= 0:
        return 0
    headroom = (node.thermal_limit_c - node.thermal_c) / node.thermal_limit_c
    score = int(round(100 * headroom))
    if node.throttled:
        score = max(0, score - 40)
    return max(0, min(100, score))


def _resource_score(req: TaskRequirements, node: NodeSnapshot) -> int:
    ram_need = max(1, req.min_ram_mb)
    ram_ratio = node.ram_free_mb / ram_need if req.min_ram_mb else min(1.0, node.ram_free_mb / 4096)
    return max(0, min(100, int(round(100 * min(1.0, ram_ratio)))))


def soft_score(
    req: TaskRequirements,
    node: NodeSnapshot,
    *,
    now: datetime | None = None,
) -> tuple[int, dict[str, int]]:
    """Deterministic integer soft score plus the labelled component breakdown."""
    components = {
        "reliability": WEIGHTS["reliability"] * max(0, min(100, node.recent_success_rate)),
        "locality": WEIGHTS["locality"] * _locality_score(req, node),
        "power": WEIGHTS["power"] * _power_score(node),
        "energy": WEIGHTS["energy"] * (0 if node.power_source == "ac" else max(0, 100 - _power_score(node))),
        "thermal": WEIGHTS["thermal"] * _thermal_score(node),
        "resource": WEIGHTS["resource"] * _resource_score(req, node),
        "latency": -WEIGHTS["latency"] * min(node.latency_ms, 5000) // 100,
        "queue": -WEIGHTS["queue"] * max(0, node.active_work),
        "retry": -WEIGHTS["retry"] * max(0, node.recent_failures),
    }
    # A preference for mains power is soft: it biases, it never disqualifies.
    if req.preferences.plugged_in and node.power_source == "ac":
        components["power"] += WEIGHTS["power"] * 25
    return sum(components.values()), components


# ---------------------------------------------------------------- placement --


@dataclass
class PlacementDecision:
    """A complete, serialisable placement verdict (with rejection evidence)."""

    job_id: str
    selected_node: str | None
    candidates: tuple[CandidateEvaluation, ...]
    reason: str
    registry_size: int = 0

    @property
    def eligible_node_ids(self) -> tuple[str, ...]:
        return tuple(c.node_id for c in self.candidates if c.eligible)

    @property
    def rejected(self) -> dict[str, tuple[str, ...]]:
        return {c.node_id: c.rejections for c in self.candidates if not c.eligible}

    def evidence(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "selected_node": self.selected_node,
            "reason": self.reason,
            "registry_size": self.registry_size,
            "candidates": [c.as_dict() for c in self.candidates],
        }

    def write_evidence(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.evidence(), indent=2, sort_keys=True) + "\n")
        return path


def evaluate_candidates(
    req: TaskRequirements,
    registry: Sequence[NodeSnapshot],
    *,
    now: datetime | None = None,
) -> tuple[CandidateEvaluation, ...]:
    """Evaluate every registry node, preserving rejection reasons.

    Ordering is stable and deterministic: eligible candidates first (higher
    score first, then ascending ``node_id``), then ineligible ones by
    ascending ``node_id``.
    """
    now = now or datetime.now(timezone.utc)
    eligible: list[CandidateEvaluation] = []
    rejected: list[CandidateEvaluation] = []
    for node in sorted(registry, key=lambda n: n.node_id):
        ok, reasons = hard_filter(req, node, now=now)
        if ok:
            score, components = soft_score(req, node, now=now)
            eligible.append(
                CandidateEvaluation(
                    node_id=node.node_id,
                    eligible=True,
                    score=score,
                    score_components=components,
                )
            )
        else:
            rejected.append(
                CandidateEvaluation(
                    node_id=node.node_id,
                    eligible=False,
                    score=None,
                    rejections=reasons,
                )
            )
    eligible.sort(key=lambda c: (-(c.score or 0), c.node_id))
    return tuple(eligible + rejected)


def select_node(
    req: TaskRequirements,
    registry: Sequence[NodeSnapshot],
    *,
    exclude: Iterable[str] = (),
    require_independent_workspace: bool = False,
    now: datetime | None = None,
) -> tuple[str | None, tuple[CandidateEvaluation, ...], str]:
    """Select the best eligible node, optionally excluding already-used workers.

    ``require_independent_workspace`` is used for concurrent placement: the
    chosen worker must carry a verified, isolated workspace identity.
    """
    excluded = set(exclude)
    nodes = {n.node_id: n for n in registry}
    candidates = evaluate_candidates(req, registry, now=now)
    for candidate in candidates:
        if not candidate.eligible or candidate.node_id in excluded:
            continue
        if require_independent_workspace:
            node = nodes[candidate.node_id]
            if not node.workspace_id or not node.identity_verified:
                continue
        return (
            candidate.node_id,
            candidates,
            f"selected {candidate.node_id} (score {candidate.score})",
        )
    if len(excluded) >= len(registry):
        reason = "no independent worker available (all workers already used in this batch)"
    else:
        reason = "no hard-eligible node: " + "; ".join(
            f"{c.node_id} [{', '.join(c.rejections)}]"
            for c in candidates
            if not c.eligible
        )
    return None, candidates, reason


def place_job(
    job_id: str,
    req: TaskRequirements,
    registry: Sequence[NodeSnapshot],
    *,
    now: datetime | None = None,
) -> PlacementDecision:
    """Deterministically select the best *hard-eligible* node for one job."""
    selected, candidates, why = select_node(req, registry, now=now)
    if selected is None:
        return PlacementDecision(
            job_id=job_id,
            selected_node=None,
            candidates=candidates,
            reason=why if candidates else "empty capability registry",
            registry_size=len(registry),
        )
    return PlacementDecision(
        job_id=job_id,
        selected_node=selected,
        candidates=candidates,
        reason=(
            f"{why}; hard-ineligible: "
            f"{[c.node_id for c in candidates if not c.eligible]}"
        ),
        registry_size=len(registry),
    )
