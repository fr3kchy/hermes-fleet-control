"""Concurrency-group scheduling, contributor assignments and lease-based work-pull.

This module sits on top of :mod:`hermes_fleet.requirements` (eligibility +
deterministic scoring) and :mod:`hermes_fleet.leases` (transactional leases,
fencing tokens, append-only event ledger).

It implements three invariants from the FR3K distributed-execution-fabric
program:

1. **Serialized by default.** Jobs are serialized unless *every* job in a batch
   explicitly declares ``concurrency.safe = True``, their concurrency groups do
   not conflict, their selected workers have independent *verified* workspace
   identities, and no job carries a global/approval/physical exclusivity gate.
   Parallelism is opt-in and evidence-backed, never inferred from the presence
   of several connected devices.
2. **One integration owner per job.** Multiple contributors to a job are
   scoped sub-assignments (their own worktree/branch) under a single integration
   owner. A contributor can commit to its own branch but cannot finalize the job
   or promote to the canonical branch.
3. **Lease-governed work-pull.** Idle verified workers *ask* the scheduler for
   eligible work; they never self-assign from Git. Ownership is granted as an
   assignment token (``lease_id:fencing_token``); duplicate, cloned or stale
   worker sessions cannot acquire or act on the same assignment.

Nothing here mutates Git lifecycle directories; it produces decisions and
leases only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .leases import LeaseConflict, LeaseManager, StaleWorker
from .requirements import (
    SERIALIZED_EXECUTION_CLASSES,
    CandidateEvaluation,
    NodeSnapshot,
    PlacementDecision,
    TaskRequirements,
    hard_filter,
    place_job,
    select_node,
)

PRIORITY_RANK: Mapping[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# Lifecycle actions reserved for the single integration owner of a job.
OWNER_ONLY_ACTIONS: frozenset[str] = frozenset(
    {"finalize", "promote", "push_main", "move_lifecycle", "claim"}
)
CONTRIBUTOR_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"commit_branch", "push_branch", "propose_change", "write_evidence"}
)


class ContributorAuthorityError(Exception):
    """Raised when a non-owner attempts an owner-only lifecycle action."""


# ----------------------------------------------------------------- scheduling --


@dataclass(frozen=True)
class ScheduledJob:
    job_id: str
    requirements: TaskRequirements
    priority: str = "P2"

    def __post_init__(self) -> None:
        if self.priority not in PRIORITY_RANK:
            raise ValueError(f"unknown priority {self.priority!r}")


def _group_resource_key(group: str, slot: int) -> str:
    return f"concurrency-group:{group}:slot{slot}"


@dataclass
class ConcurrencyPlan:
    """Deterministic placement + concurrency decision for a set of jobs."""

    mode: str  # "serialized" | "parallel"
    batches: tuple[tuple[str, ...], ...]
    assignments: Mapping[str, str]  # job_id -> node_id
    decisions: Mapping[str, PlacementDecision]
    resource_keys: Mapping[str, str]  # job_id -> exclusive lease resource
    reasons: tuple[str, ...] = ()

    @property
    def serialized(self) -> bool:
        return self.mode == "serialized"

    def evidence(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "batches": [list(b) for b in self.batches],
            "assignments": dict(self.assignments),
            "resource_keys": dict(self.resource_keys),
            "reasons": list(self.reasons),
            "placement": {jid: d.evidence() for jid, d in self.decisions.items()},
        }


def _can_join_batch(
    batch: Sequence[str],
    candidate: str,
    reqs: Mapping[str, TaskRequirements],
) -> tuple[bool, str]:
    """Requirement-level compatibility of ``candidate`` with ``batch``.

    Worker-level constraints (independent verified workspace identity) are
    enforced during node selection, not here.
    """
    creq = reqs[candidate]
    if not creq.concurrency.safe:
        return False, "job does not declare concurrency_safe=true (default serialized)"
    if creq.concurrency.exclusive:
        return False, "job declares an exclusive/global-exclusive gate"
    if creq.execution_class in SERIALIZED_EXECUTION_CLASSES:
        return False, f"{creq.execution_class} execution class requires serialization"

    for other in batch:
        oreq = reqs[other]
        if not oreq.concurrency.safe:
            return False, f"conflicts with already-serialized job {other}"
        if oreq.concurrency.exclusive:
            return False, f"conflicts with exclusive job {other}"
        if oreq.execution_class in SERIALIZED_EXECUTION_CLASSES:
            return False, (
                f"conflicts with {other} ({oreq.execution_class} execution class "
                "requires serialization)"
            )
        if oreq.concurrency.group == creq.concurrency.group:
            limit = min(oreq.concurrency.max_parallelism, creq.concurrency.max_parallelism)
            in_group = sum(
                1 for j in batch if reqs[j].concurrency.group == creq.concurrency.group
            )
            if in_group + 1 > limit:
                return False, (
                    f"concurrency group {creq.concurrency.group!r} already at "
                    f"max_parallelism {limit}"
                )
    return True, ""


def plan_concurrency(
    jobs: Sequence[ScheduledJob],
    registry: Sequence[NodeSnapshot],
    *,
    now: datetime | None = None,
) -> ConcurrencyPlan:
    """Place jobs and decide which may run concurrently.

    Deterministic: jobs are ordered by priority then job_id, and the greedy
    batching is a pure function of ``(jobs, registry, now)``. A job that may
    not join the open batch starts a new batch (default serialized); a job that
    *may* join is placed on the best eligible worker not already used by that
    batch, so concurrent jobs always sit on independent workers.
    """
    reqs = {j.job_id: j.requirements for j in jobs}
    ordered = sorted(jobs, key=lambda j: (PRIORITY_RANK[j.priority], j.job_id))

    decisions: dict[str, PlacementDecision] = {}
    assignments: dict[str, str] = {}
    reasons: list[str] = []
    batches: list[list[str]] = []
    current: list[str] = []
    used: set[str] = set()

    def _open_batch(job: ScheduledJob) -> None:
        nonlocal current, used
        selected, candidates, why = select_node(job.requirements, registry, now=now)
        if selected is None:
            batches.append([job.job_id])
            decisions[job.job_id] = PlacementDecision(
                job.job_id, None, candidates, why, registry_size=len(registry)
            )
            reasons.append(f"{job.job_id}: unplaced, no hard-eligible node ({why})")
            current, used = [], set()
            return
        current, used = [job.job_id], {selected}
        assignments[job.job_id] = selected
        decisions[job.job_id] = PlacementDecision(
            job.job_id, selected, candidates, why, registry_size=len(registry)
        )
        if not job.requirements.concurrency.safe:
            reasons.append(
                f"{job.job_id}: default serialized (concurrency_safe is false)"
            )

    for job in ordered:
        jid = job.job_id
        if not current:
            _open_batch(job)
            continue

        ok, why = _can_join_batch(current, jid, reqs)
        selected: str | None = None
        candidates: tuple[CandidateEvaluation, ...] = ()
        sel_why = ""
        if ok:
            selected, candidates, sel_why = select_node(
                job.requirements,
                registry,
                exclude=used,
                require_independent_workspace=True,
                now=now,
            )
            if selected is None:
                ok, why = False, (
                    "no independent eligible worker with a verified workspace "
                    f"identity available ({sel_why})"
                )
        if ok and selected is not None:
            current.append(jid)
            used.add(selected)
            assignments[jid] = selected
            decisions[jid] = PlacementDecision(
                jid, selected, candidates, sel_why, registry_size=len(registry)
            )
            continue

        reasons.append(f"{jid}: serialized after {current} — {why}")
        batches.append(current)
        current, used = [], set()
        _open_batch(job)

    if current:
        batches.append(current)

    resource_keys: dict[str, str] = {}
    for batch in batches:
        slots: dict[str, int] = {}
        for jid in batch:
            group = reqs[jid].concurrency.group
            slot = slots.get(group, 0)
            slots[group] = slot + 1
            resource_keys[jid] = _group_resource_key(group, slot)

    mode = "parallel" if any(len(b) > 1 for b in batches) else "serialized"
    return ConcurrencyPlan(
        mode=mode,
        batches=tuple(tuple(b) for b in batches),
        assignments=assignments,
        decisions=decisions,
        resource_keys=resource_keys,
        reasons=tuple(reasons),
    )


# ----------------------------------------------------- contributor contracts --


@dataclass(frozen=True)
class WorkerRef:
    device_id: str
    agent_id: str
    attempt_id: str
    workspace_id: str

    @property
    def identity_key(self) -> str:
        return f"{self.device_id}:{self.agent_id}"


def contributor_branch(ref: WorkerRef, job_id: str) -> str:
    """Canonical per-contributor branch namespace (doctrine: work/<...>)."""
    return f"work/{job_id}/{ref.device_id}/{ref.agent_id}/{ref.attempt_id}"


@dataclass(frozen=True)
class ContributorAssignment:
    job_id: str
    role: str  # "integration_owner" | "contributor"
    worker: WorkerRef
    integration_owner: str
    branch: str

    @property
    def can_finalize(self) -> bool:
        return self.role == "integration_owner"

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "role": self.role,
            "worker": {
                "device_id": self.worker.device_id,
                "agent_id": self.worker.agent_id,
                "attempt_id": self.worker.attempt_id,
                "workspace_id": self.worker.workspace_id,
            },
            "integration_owner": self.integration_owner,
            "branch": self.branch,
            "can_finalize": self.can_finalize,
        }


@dataclass(frozen=True)
class ContributorContract:
    job_id: str
    integration_owner: str
    assignments: tuple[ContributorAssignment, ...]

    @property
    def contributors(self) -> tuple[ContributorAssignment, ...]:
        return tuple(a for a in self.assignments if a.role == "contributor")

    def assignment_for(self, device_id: str, agent_id: str, attempt_id: str) -> ContributorAssignment | None:
        for a in self.assignments:
            w = a.worker
            if (w.device_id, w.agent_id, w.attempt_id) == (device_id, agent_id, attempt_id):
                return a
        return None


def assign_contributors(
    job_id: str,
    *,
    integration_owner: WorkerRef,
    contributors: Sequence[WorkerRef] = (),
) -> ContributorContract:
    """Issue scoped contributor assignments under exactly one integration owner.

    Enforces: exactly one owner, unique worker identity per assignment, and a
    unique branch/workspace namespace per contributor (so two agents on one
    device can never collide).
    """
    seen_identities: set[tuple[str, str, str]] = set()
    seen_branches: set[str] = set()
    seen_workspaces: set[str] = set()
    assignments: list[ContributorAssignment] = []

    def _register(ref: WorkerRef, role: str) -> None:
        key = (ref.device_id, ref.agent_id, ref.attempt_id)
        if key in seen_identities:
            raise ValueError(f"duplicate worker identity in contributor assignment: {key}")
        branch = contributor_branch(ref, job_id)
        if branch in seen_branches:
            raise ValueError(f"duplicate contributor branch: {branch}")
        if not ref.workspace_id:
            raise ValueError("contributor assignment requires a workspace identity")
        if ref.workspace_id in seen_workspaces:
            raise ValueError(
                f"workspace {ref.workspace_id!r} reused; contributors need independent workspaces"
            )
        seen_identities.add(key)
        seen_branches.add(branch)
        seen_workspaces.add(ref.workspace_id)
        assignments.append(
            ContributorAssignment(
                job_id=job_id,
                role=role,
                worker=ref,
                integration_owner=integration_owner.identity_key,
                branch=branch,
            )
        )

    _register(integration_owner, "integration_owner")
    for ref in contributors:
        if ref.identity_key == integration_owner.identity_key:
            raise ValueError(
                "contributor shares the integration owner's worker identity "
                f"{ref.identity_key}; contributors must be distinct workers"
            )
        _register(ref, "contributor")

    return ContributorContract(
        job_id=job_id,
        integration_owner=integration_owner.identity_key,
        assignments=tuple(assignments),
    )


def guard_lifecycle_action(assignment: ContributorAssignment, action: str) -> None:
    """Fail closed when a contributor attempts an owner-only lifecycle action."""
    if action in OWNER_ONLY_ACTIONS and not assignment.can_finalize:
        raise ContributorAuthorityError(
            f"{assignment.worker.identity_key} is a contributor on job "
            f"{assignment.job_id} and may not perform {action!r}; only the single "
            f"integration owner ({assignment.integration_owner}) may finalize/promote"
        )
    if action not in OWNER_ONLY_ACTIONS and action not in CONTRIBUTOR_ALLOWED_ACTIONS:
        raise ContributorAuthorityError(f"unknown lifecycle action {action!r}")


# --------------------------------------------------------------- work-pull ----


@dataclass(frozen=True)
class WorkerIdentity:
    node_id: str
    agent_id: str
    attempt_id: str
    workspace_id: str
    session_id: str

    @property
    def identity_key(self) -> str:
        return f"{self.node_id}:{self.agent_id}"

    @property
    def worker_id(self) -> str:
        return f"{self.node_id}:{self.agent_id}:{self.attempt_id}"


@dataclass
class PullResult:
    granted: bool
    reason: str
    job_id: str | None = None
    node_id: str | None = None
    lease_id: str | None = None
    fencing_token: int | None = None
    assignment_token: str | None = None
    resource_key: str | None = None
    candidates: tuple[CandidateEvaluation, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "reason": self.reason,
            "job_id": self.job_id,
            "node_id": self.node_id,
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
            "assignment_token": self.assignment_token,
            "resource_key": self.resource_key,
            "candidates": [c.as_dict() for c in self.candidates],
        }


class WorkPullScheduler:
    """The sole scheduler-side work-pull endpoint.

    Idle workers ask for work; the scheduler decides eligibility, grants an
    exclusive lease (assignment token) and records why alternatives were
    skipped. Workers never select a different global job by themselves.
    """

    def __init__(
        self,
        registry: Sequence[NodeSnapshot],
        lease_manager: LeaseManager,
        jobs: Sequence[ScheduledJob],
        *,
        lease_ttl_seconds: int = 300,
    ) -> None:
        self.registry = {n.node_id: n for n in registry}
        self.leases = lease_manager
        self.jobs = tuple(jobs)
        self.lease_ttl_seconds = lease_ttl_seconds
        self._live_sessions: dict[str, str] = {}

    # -- session registry (cloned/duplicate identity containment) -----------

    def register_session(self, worker: WorkerIdentity) -> None:
        """Register a live worker session; refuse a cloned/duplicate identity."""
        existing = self._live_sessions.get(worker.identity_key)
        if existing is not None and existing != worker.session_id:
            raise ContributorAuthorityError(
                f"duplicate live worker identity {worker.identity_key}: session "
                f"{worker.session_id} conflicts with live session {existing}; "
                "quarantine and reprovision rather than granting work"
            )
        self._live_sessions[worker.identity_key] = worker.session_id

    def release_session(self, worker: WorkerIdentity) -> None:
        if self._live_sessions.get(worker.identity_key) == worker.session_id:
            del self._live_sessions[worker.identity_key]

    # -- work pull ----------------------------------------------------------

    def request_work(
        self, worker: WorkerIdentity, *, now: datetime | None = None
    ) -> PullResult:
        node = self.registry.get(worker.node_id)
        if node is None:
            return PullResult(
                granted=False,
                reason=f"unknown worker node {worker.node_id!r}; not in verified registry",
            )
        if not worker.workspace_id:
            return PullResult(
                granted=False,
                reason="worker session has no isolated workspace identity; refusing work",
            )
        live = self._live_sessions.get(worker.identity_key)
        if live is not None and live != worker.session_id:
            return PullResult(
                granted=False,
                reason=(
                    f"duplicate/cloned worker identity {worker.identity_key}: live "
                    f"session {live} already owns this worker; refusing work"
                ),
            )

        ordered = sorted(
            self.jobs, key=lambda j: (PRIORITY_RANK[j.priority], j.job_id)
        )
        evaluations: list[CandidateEvaluation] = []
        chosen: ScheduledJob | None = None
        for job in ordered:
            ok, reasons = hard_filter(job.requirements, node, now=now)
            evaluations.append(
                CandidateEvaluation(
                    node_id=node.node_id, eligible=ok, score=None, rejections=reasons
                )
            )
            if ok and chosen is None:
                chosen = job

        if chosen is None:
            return PullResult(
                granted=False,
                reason=(
                    f"worker {worker.worker_id} is not hard-eligible for any queued job"
                ),
                candidates=tuple(evaluations),
            )

        resource_key = _group_resource_key(chosen.requirements.concurrency.group, 0)
        idempotency_key = f"{chosen.job_id}:{worker.identity_key}:{worker.attempt_id}"
        try:
            lease = self.leases.grant(
                chosen.job_id,
                worker.node_id,
                execution_class=chosen.requirements.execution_class,
                ttl_seconds=self.lease_ttl_seconds,
                resources=(resource_key,),
                attempt_id=worker.attempt_id,
                idempotency_key=idempotency_key,
            )
        except LeaseConflict as exc:
            return PullResult(
                granted=False,
                reason=f"assignment already owned: {exc}",
                job_id=chosen.job_id,
                candidates=tuple(evaluations),
            )

        self._live_sessions[worker.identity_key] = worker.session_id
        return PullResult(
            granted=True,
            reason=f"granted {chosen.job_id} to {worker.worker_id}",
            job_id=chosen.job_id,
            node_id=worker.node_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            assignment_token=f"{lease.lease_id}:{lease.fencing_token}",
            resource_key=resource_key,
            candidates=tuple(evaluations),
        )
    # -- protected lifecycle boundaries ------------------------------------

    def heartbeat(self, result: PullResult) -> None:
        """Renew the lease. A stale/expired/unowned assignment raises StaleWorker."""
        if (
            not result.granted
            or not result.job_id
            or not result.lease_id
            or result.fencing_token is None
        ):
            raise StaleWorker("cannot renew an assignment that was never granted")
        self.leases.renew(
            result.job_id, result.lease_id, result.fencing_token, ttl_seconds=self.lease_ttl_seconds
        )

    def complete(
        self,
        result: PullResult,
        *,
        final_status: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit a terminal result; rejects any non-current fence owner."""
        if (
            not result.granted
            or not result.job_id
            or not result.lease_id
            or result.fencing_token is None
        ):
            raise StaleWorker("cannot complete an assignment that was never granted")
        return self.leases.submit_result(
            result.job_id,
            result.lease_id,
            result.fencing_token,
            final_status=final_status,
            evidence=dict(evidence or {}),
        )


__all__ = [
    "ConcurrencyPlan",
    "ContributorAssignment",
    "ContributorAuthorityError",
    "ContributorContract",
    "OWNER_ONLY_ACTIONS",
    "PRIORITY_RANK",
    "PullResult",
    "ScheduledJob",
    "WorkerIdentity",
    "WorkerRef",
    "WorkPullScheduler",
    "assign_contributors",
    "contributor_branch",
    "guard_lifecycle_action",
    "plan_concurrency",
]
