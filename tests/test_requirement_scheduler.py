"""Acceptance tests for the requirement contract, deterministic placement,
concurrency-group scheduling, contributor authority and lease-based work-pull.

Each test class maps to a declared acceptance criterion of job
20260911T181858Z-implement-requirement-scheduler.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_fleet.leases import LeaseConflict, LeaseManager, StaleWorker, utcnow
from hermes_fleet.placement import (
    ContributorAuthorityError,
    ScheduledJob,
    WorkerIdentity,
    WorkerRef,
    WorkPullScheduler,
    assign_contributors,
    guard_lifecycle_action,
    plan_concurrency,
)
from hermes_fleet.requirements import (
    CapabilityNeed,
    ConcurrencySpec,
    NodeCapability,
    NodeSnapshot,
    Preferences,
    TaskRequirements,
    evaluate_candidates,
    place_job,
    soft_score,
)

NOW = datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ helpers --


def node(node_id: str = "gpd", **overrides) -> NodeSnapshot:
    base: dict = {
        "node_id": node_id,
        "device_class": "linux-handheld",
        "os": "fedora",
        "architecture": "x86_64",
        "trust_tier": 3,
        "allowed_execution_classes": ("AUTO", "OBSERVE", "APPROVAL", "PHYSICAL"),
        "capabilities": {"compute.python": NodeCapability("compute.python", "curator_verified")},
        "ram_free_mb": 8192,
        "storage_free_mb": 100_000,
        "transports": ("lan",),
        "latency_ms": 5,
        "power_source": "ac",
        "battery_pct": 100,
        "charging": True,
        "thermal_c": 40.0,
        "thermal_limit_c": 95.0,
        "identity_verified": True,
        "workspace_id": f"ws-{node_id}",
        "active_work": 0,
        "recent_success_rate": 100,
        "recent_failures": 0,
    }
    base.update(overrides)
    return NodeSnapshot(**base)


def req(**overrides) -> TaskRequirements:
    base: dict = {"capabilities": (CapabilityNeed("compute.python", "probe_verified"),)}
    base.update(overrides)
    return TaskRequirements(**base)


def job(job_id: str, priority: str = "P2", **overrides) -> ScheduledJob:
    return ScheduledJob(job_id=job_id, requirements=req(**overrides), priority=priority)


# ------------------------------------------------------- AC1 hard filtering ---


class TestHardFilterPrecedesScoring:
    """Hard-ineligible nodes can never win soft scoring."""

    def test_ineligible_but_fastest_node_cannot_be_selected(self):
        # The "fast" node is untrusted (T2 < required T3) but would otherwise
        # out-score the qualified node on every soft dimension.
        unqualified = node(
            "fast-untrusted",
            trust_tier=2,
            latency_ms=1,
            active_work=0,
            recent_success_rate=100,
            ram_free_mb=65536,
            thermal_c=30.0,
        )
        qualified = node("qualified-slow", latency_ms=400, active_work=3, thermal_c=80.0)
        decision = place_job("job-1", req(trust_level_min=3), [unqualified, qualified], now=NOW)
        assert decision.selected_node == "qualified-slow"
        assert "fast-untrusted" not in decision.eligible_node_ids
        assert any("trust tier" in r for r in decision.rejected["fast-untrusted"])

    def test_every_hard_constraint_is_recorded_as_a_rejection_reason(self):
        registry = [
            node("offline", online=False),
            node("quarantined", quarantined=True),
            node("unverified", identity_verified=False),
            node("wrong-os", os="android"),
            node("low-ram", ram_free_mb=1024),
            node("low-storage", storage_free_mb=10),
            node(
                "weak-capability",
                capabilities={"compute.python": NodeCapability("compute.python", "self_declared")},
            ),
            node("expired-capability", capabilities={
                "compute.python": NodeCapability(
                    "compute.python", "curator_verified", expires_at="2026-09-11T00:00:00+00:00"
                )
            }),
            node("throttled-to-limit", thermal_c=95.0),
            node("critical-battery", power_source="battery", charging=False, battery_pct=5),
        ]
        r = req(os=("fedora",), min_ram_mb=4096, min_storage_mb=1000)
        evaluations = {c.node_id: c for c in evaluate_candidates(r, registry, now=NOW)}
        for node_id in ("offline", "quarantined", "unverified", "wrong-os", "low-ram",
                        "low-storage", "weak-capability", "expired-capability",
                        "throttled-to-limit", "critical-battery"):
            assert evaluations[node_id].eligible is False, node_id
            assert evaluations[node_id].rejections, node_id
        assert place_job("job-1", r, registry, now=NOW).selected_node is None


# ---------------------------------------------------------- AC2 determinism --


class TestDeterministicPlacement:
    """Same registry/task snapshot yields deterministic placement."""

    def test_same_snapshot_yields_identical_evidence(self):
        registry = [
            node("c"), node("a", latency_ms=100, active_work=1),
            node("b", latency_ms=20, recent_failures=1),
        ]
        first = place_job("job-1", req(), registry, now=NOW)
        second = place_job("job-1", req(), registry, now=NOW)
        assert json.dumps(first.evidence(), sort_keys=True) == json.dumps(
            second.evidence(), sort_keys=True
        )

    def test_registry_ordering_and_ties_do_not_change_result(self):
        registry = [node("a"), node("b"), node("c")]
        forward = place_job("job-1", req(), registry, now=NOW)
        shuffled = place_job("job-1", req(), list(reversed(registry)), now=NOW)
        # Identical soft scores -> deterministic tie-break on node_id.
        assert forward.selected_node == shuffled.selected_node == "a"
        assert [c.node_id for c in forward.candidates] == [
            c.node_id for c in shuffled.candidates
        ]

    def test_higher_score_wins_and_breakdown_is_retained(self):
        best = node("best", active_work=0, thermal_c=30.0, recent_success_rate=100)
        worse = node("worse", active_work=4, thermal_c=90.0, recent_success_rate=50)
        decision = place_job("job-1", req(), [worse, best], now=NOW)
        assert decision.selected_node == "best"
        chosen = next(c for c in decision.candidates if c.node_id == "best")
        assert chosen.score_components["queue"] == 0
        assert chosen.score_components["reliability"] > 0
        assert chosen.score is not None and chosen.score > 0

    def test_scoring_is_pure_and_reproducible(self):
        n = node("x")
        assert soft_score(req(), n, now=NOW) == soft_score(req(), n, now=NOW)


# --------------------------------------------- AC3 power/thermal/locality ----


class TestPlacementReactsToState:
    """Battery/thermal/locality changes alter placement as expected."""

    def test_power_source_changes_placement(self):
        a = node("a", power_source="ac")
        b = node("b", power_source="battery", charging=False, battery_pct=60)
        assert place_job("j", req(), [a, b], now=NOW).selected_node == "a"
        # Flip the power state; placement must follow the snapshot, not identity.
        a2 = node("a", power_source="battery", charging=False, battery_pct=60)
        b2 = node("b", power_source="ac")
        assert place_job("j", req(), [a2, b2], now=NOW).selected_node == "b"

    def test_battery_level_changes_placement(self):
        high = node("high", power_source="battery", charging=False, battery_pct=90)
        low = node("low", power_source="battery", charging=False, battery_pct=40)
        assert place_job("j", req(), [low, high], now=NOW).selected_node == "high"

    def test_thermal_headroom_changes_placement(self):
        cool = node("cool", thermal_c=40.0)
        hot = node("hot", thermal_c=90.0)
        assert place_job("j", req(), [hot, cool], now=NOW).selected_node == "cool"
        cool2 = node("cool", thermal_c=93.0)
        hot2 = node("hot", thermal_c=60.0)
        assert place_job("j", req(), [hot2, cool2], now=NOW).selected_node == "hot"

    def test_locality_changes_placement(self):
        refs = ("object://capture/sha256/abc",)
        local = node("local", data_refs=refs)
        remote = node("remote", data_refs=())
        r = req(data_refs=refs, locality_required=False)
        assert place_job("j", r, [remote, local], now=NOW).selected_node == "local"
        local2 = node("local", data_refs=())
        remote2 = node("remote", data_refs=refs)
        assert place_job("j", r, [remote2, local2], now=NOW).selected_node == "remote"

    def test_required_locality_is_a_hard_constraint(self):
        refs = ("object://capture/sha256/abc",)
        r = req(data_refs=refs, locality_required=True)
        decision = place_job("j", r, [node("remote", data_refs=())], now=NOW)
        assert decision.selected_node is None
        assert any("required local data absent" in x for x in decision.rejected["remote"])


# ---------------------------------------------------- AC4 instrument-only ----


class TestInstrumentOnlyDevices:
    """Instrument-only devices are never scheduled as generic compute workers."""

    def test_instrument_only_never_selected_as_compute_worker(self):
        instrument = node(
            "nanovna-host",
            device_class="instrument",
            latency_ms=1,
            active_work=0,
            thermal_c=25.0,
            ram_free_mb=65536,
        )
        decision = place_job("j", req(), [instrument], now=NOW)
        assert decision.selected_node is None
        assert any(
            "instrument-only" in r for r in decision.rejected["nanovna-host"]
        )

    def test_instrument_loses_to_a_weaker_real_worker(self):
        instrument = node("instrument", device_class="esp32", latency_ms=1, thermal_c=25.0)
        worker = node("worker", latency_ms=900, active_work=5, thermal_c=90.0)
        decision = place_job("j", req(), [instrument, worker], now=NOW)
        assert decision.selected_node == "worker"
        assert "instrument" not in decision.eligible_node_ids

    def test_explicit_opt_in_allows_instrument_worker(self):
        instrument = node("instrument", device_class="instrument")
        r = req(allow_instrument_worker=True)
        assert place_job("j", r, [instrument], now=NOW).selected_node == "instrument"


# ------------------------------------------------ AC5 opt-in concurrency -----


class TestConcurrencyScheduling:
    """Default jobs stay serialized; concurrency is opt-in and evidence-backed."""

    def test_default_jobs_are_serialized(self):
        registry = [node("a"), node("b"), node("c")]
        plan = plan_concurrency(
            [job("j1"), job("j2"), job("j3")], registry, now=NOW
        )
        assert plan.mode == "serialized"
        assert all(len(batch) == 1 for batch in plan.batches)
        assert any("default serialized" in r for r in plan.reasons)

    def test_explicitly_safe_nonconflicting_groups_run_in_parallel(self):
        registry = [node("a"), node("b")]
        jobs = [
            job("j1", concurrency=ConcurrencySpec(group="g1", safe=True)),
            job("j2", concurrency=ConcurrencySpec(group="g2", safe=True)),
        ]
        plan = plan_concurrency(jobs, registry, now=NOW)
        assert plan.mode == "parallel"
        assert plan.batches == (("j1", "j2"),)
        assert plan.assignments == {"j1": "a", "j2": "b"}
        assert plan.resource_keys["j1"] != plan.resource_keys["j2"]

    def test_one_unsafe_job_keeps_the_batch_serialized(self):
        registry = [node("a"), node("b")]
        jobs = [
            job("j1", concurrency=ConcurrencySpec(group="g1", safe=True)),
            job("j2", concurrency=ConcurrencySpec(group="g2", safe=False)),
        ]
        plan = plan_concurrency(jobs, registry, now=NOW)
        assert plan.mode == "serialized"
        assert all(len(b) == 1 for b in plan.batches)

    def test_same_group_beyond_max_parallelism_is_serialized(self):
        registry = [node("a"), node("b")]
        jobs = [
            job("j1", concurrency=ConcurrencySpec(group="rf", safe=True, max_parallelism=1)),
            job("j2", concurrency=ConcurrencySpec(group="rf", safe=True, max_parallelism=1)),
        ]
        plan = plan_concurrency(jobs, registry, now=NOW)
        assert plan.mode == "serialized"
        assert any("max_parallelism" in r for r in plan.reasons)

    def test_same_group_within_max_parallelism_runs_parallel(self):
        registry = [node("a"), node("b")]
        jobs = [
            job("j1", concurrency=ConcurrencySpec(group="rf", safe=True, max_parallelism=2)),
            job("j2", concurrency=ConcurrencySpec(group="rf", safe=True, max_parallelism=2)),
        ]
        plan = plan_concurrency(jobs, registry, now=NOW)
        assert plan.mode == "parallel"
        assert len(plan.batches[0]) == 2

    def test_missing_workspace_identity_forces_serialization(self):
        registry = [node("a"), node("b", workspace_id=None)]
        jobs = [
            job("j1", concurrency=ConcurrencySpec(group="g1", safe=True)),
            job("j2", concurrency=ConcurrencySpec(group="g2", safe=True)),
        ]
        plan = plan_concurrency(jobs, registry, now=NOW)
        assert plan.mode == "serialized"
        # j2 is placed on the workspace-less node b and cannot run concurrently.
        assert any("workspace identity" in r for r in plan.reasons)

    def test_approval_class_always_serializes(self):
        registry = [node("a"), node("b")]
        jobs = [
            job("j1", execution_class="APPROVAL", concurrency=ConcurrencySpec(group="g1", safe=True)),
            job("j2", concurrency=ConcurrencySpec(group="g2", safe=True)),
        ]
        plan = plan_concurrency(jobs, registry, now=NOW)
        assert plan.mode == "serialized"
        assert any("requires serialization" in r for r in plan.reasons)

    def test_exclusive_gate_serializes(self):
        registry = [node("a"), node("b")]
        jobs = [
            job("j1", concurrency=ConcurrencySpec(group="g1", safe=True, exclusive=True)),
            job("j2", concurrency=ConcurrencySpec(group="g2", safe=True)),
        ]
        plan = plan_concurrency(jobs, registry, now=NOW)
        assert plan.mode == "serialized"
        assert any("exclusive" in r for r in plan.reasons)


# ------------------------------------------------- AC6 contributor authority -


class TestContributorAuthority:
    """Two contributors to one job cannot independently finalize/promote."""

    def test_exactly_one_integration_owner_can_finalize(self):
        owner = WorkerRef("gpd-win-mini", "hermes-a", "att-1", "ws-owner")
        c1 = WorkerRef("gpd-win-mini", "hermes-b", "att-2", "ws-c1")
        c2 = WorkerRef("desktop-parrot", "hermes-c", "att-3", "ws-c2")
        contract = assign_contributors("job-1", integration_owner=owner, contributors=[c1, c2])
        assert contract.integration_owner == "gpd-win-mini:hermes-a"
        assert sum(1 for a in contract.assignments if a.can_finalize) == 1
        assert contract.assignment_for("gpd-win-mini", "hermes-a", "att-1").can_finalize
        assert not contract.assignment_for("gpd-win-mini", "hermes-b", "att-2").can_finalize

    def test_contributor_finalize_and_promote_are_refused(self):
        owner = WorkerRef("gpd-win-mini", "hermes-a", "att-1", "ws-owner")
        contributor = WorkerRef("gpd-win-mini", "hermes-b", "att-2", "ws-c1")
        contract = assign_contributors("job-1", integration_owner=owner, contributors=[contributor])
        assignment = contract.contributors[0]
        for action in ("finalize", "promote", "push_main", "move_lifecycle"):
            with pytest.raises(ContributorAuthorityError):
                guard_lifecycle_action(assignment, action)
        # The contributor may still work on its own scoped branch/evidence.
        guard_lifecycle_action(assignment, "commit_branch")
        guard_lifecycle_action(assignment, "write_evidence")

    def test_owner_may_finalize(self):
        owner = WorkerRef("gpd-win-mini", "hermes-a", "att-1", "ws-owner")
        contract = assign_contributors("job-1", integration_owner=owner)
        guard_lifecycle_action(contract.assignments[0], "finalize")

    def test_contributor_branches_are_unique_namespaces(self):
        owner = WorkerRef("gpd-win-mini", "hermes-a", "att-1", "ws-owner")
        c1 = WorkerRef("gpd-win-mini", "hermes-b", "att-2", "ws-c1")
        contract = assign_contributors("job-1", integration_owner=owner, contributors=[c1])
        assert contract.contributors[0].branch == "work/job-1/gpd-win-mini/hermes-b/att-2"
        branches = [a.branch for a in contract.assignments]
        assert len(set(branches)) == len(branches) == 2

    def test_duplicate_worker_identity_is_refused(self):
        owner = WorkerRef("gpd-win-mini", "hermes-a", "att-1", "ws-owner")
        dup = WorkerRef("gpd-win-mini", "hermes-a", "att-1", "ws-dup")
        with pytest.raises(ValueError):
            assign_contributors("job-1", integration_owner=owner, contributors=[dup])

    def test_contributor_cannot_reuse_the_owner_identity(self):
        owner = WorkerRef("gpd-win-mini", "hermes-a", "att-1", "ws-owner")
        same_identity = WorkerRef("gpd-win-mini", "hermes-a", "att-9", "ws-x")
        with pytest.raises(ValueError):
            assign_contributors("job-1", integration_owner=owner, contributors=[same_identity])

    def test_contributors_need_independent_workspaces(self):
        owner = WorkerRef("gpd-win-mini", "hermes-a", "att-1", "ws-owner")
        c1 = WorkerRef("gpd-win-mini", "hermes-b", "att-2", "ws-shared")
        c2 = WorkerRef("gpd-win-mini", "hermes-c", "att-3", "ws-shared")
        with pytest.raises(ValueError):
            assign_contributors("job-1", integration_owner=owner, contributors=[c1, c2])


# ------------------------------------------------ AC7 assignment exclusivity -


class TestAssignmentTokens:
    """Duplicate/stale worker sessions cannot acquire the same assignment token."""

    @pytest.fixture()
    def scheduler(self, tmp_path: Path) -> WorkPullScheduler:
        mgr = LeaseManager(tmp_path / "leases.db")
        return WorkPullScheduler(
            [node("gpd")], mgr, [job("j1", priority="P0")], lease_ttl_seconds=60
        )

    def test_two_sessions_cannot_hold_the_same_assignment(self, scheduler: WorkPullScheduler):
        first = scheduler.request_work(
            WorkerIdentity("gpd", "agent-1", "att-1", "ws-1", "sess-1")
        )
        assert first.granted and first.assignment_token
        second = scheduler.request_work(
            WorkerIdentity("gpd", "agent-2", "att-2", "ws-2", "sess-2")
        )
        assert second.granted is False
        assert "already owned" in second.reason
        assert second.assignment_token is None

    def test_cloned_identity_session_is_refused(self, scheduler: WorkPullScheduler):
        scheduler.register_session(WorkerIdentity("gpd", "agent-1", "att-1", "ws-1", "sess-1"))
        with pytest.raises(ContributorAuthorityError):
            scheduler.register_session(
                WorkerIdentity("gpd", "agent-1", "att-1", "ws-2", "sess-2")
            )

    def test_stale_session_cannot_renew_after_reassignment(self, tmp_path: Path):
        mgr = LeaseManager(tmp_path / "leases.db")
        scheduler = WorkPullScheduler(
            [node("gpd")], mgr, [job("j1", priority="P0")], lease_ttl_seconds=60
        )
        stale = scheduler.request_work(
            WorkerIdentity("gpd", "agent-1", "att-1", "ws-1", "sess-1")
        )
        assert stale.granted
        mgr.expire_due(now=utcnow() + timedelta(seconds=120))
        # A new attempt is granted a strictly higher fence token.
        fresh = scheduler.request_work(
            WorkerIdentity("gpd", "agent-2", "att-2", "ws-2", "sess-2")
        )
        assert fresh.granted and fresh.fencing_token > stale.fencing_token
        with pytest.raises(StaleWorker):
            scheduler.heartbeat(stale)
        with pytest.raises(StaleWorker):
            scheduler.complete(stale, final_status="completed")
        # The current owner can complete.
        scheduler.complete(fresh, final_status="completed", evidence={"verified": True})

    def test_ineligible_worker_gets_no_assignment(self, tmp_path: Path):
        mgr = LeaseManager(tmp_path / "leases.db")
        scheduler = WorkPullScheduler(
            [node("esp", device_class="instrument")], mgr, [job("j1")], lease_ttl_seconds=60
        )
        result = scheduler.request_work(
            WorkerIdentity("esp", "agent-1", "att-1", "ws-1", "sess-1")
        )
        assert result.granted is False
        assert "not hard-eligible" in result.reason
        assert result.candidates and result.candidates[0].rejections

    def test_unknown_worker_node_is_refused(self, scheduler: WorkPullScheduler):
        result = scheduler.request_work(
            WorkerIdentity("ghost", "agent-1", "att-1", "ws-1", "sess-1")
        )
        assert result.granted is False
        assert "unknown worker node" in result.reason


# --------------------------------------------------------- AC8 evidence ------


class TestDecisionEvidence:
    """Placement/concurrency decisions retain candidate/rejection evidence."""

    def test_placement_evidence_lists_all_candidates_and_reasons(self, tmp_path: Path):
        registry = [node("good"), node("bad", trust_tier=0)]
        decision = place_job("j1", req(), registry, now=NOW)
        evidence = decision.evidence()
        assert evidence["selected_node"] == "good"
        assert {c["node_id"] for c in evidence["candidates"]} == {"good", "bad"}
        bad = next(c for c in evidence["candidates"] if c["node_id"] == "bad")
        assert bad["eligible"] is False and bad["rejections"]
        path = decision.write_evidence(tmp_path / "evidence" / "j1.json")
        assert json.loads(path.read_text())["selected_node"] == "good"

    def test_concurrency_evidence_retains_serialization_reasons(self):
        registry = [node("a"), node("b")]
        plan = plan_concurrency(
            [job("j1"), job("j2", concurrency=ConcurrencySpec(group="g", safe=True))],
            registry,
            now=NOW,
        )
        evidence = plan.evidence()
        assert evidence["reasons"]
        assert "placement" in evidence and "j1" in evidence["placement"]

    def test_pull_result_retains_candidate_evidence(self, tmp_path: Path):
        mgr = LeaseManager(tmp_path / "leases.db")
        scheduler = WorkPullScheduler(
            [node("gpd")],
            mgr,
            [job("j1"), job("j2", min_ram_mb=10**9)],
            lease_ttl_seconds=60,
        )
        result = scheduler.request_work(
            WorkerIdentity("gpd", "agent-1", "att-1", "ws-1", "sess-1")
        )
        assert result.granted
        payload = result.as_dict()
        assert payload["assignment_token"] == f"{payload['lease_id']}:{payload['fencing_token']}"
        assert len(payload["candidates"]) == 2
