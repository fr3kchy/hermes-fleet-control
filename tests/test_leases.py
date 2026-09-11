"""Acceptance tests for transactional leases, fencing and the event ledger.

Each test maps to a declared acceptance criterion of job
20260911T181859Z-implement-leases-fencing-event-ledger.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from hermes_fleet.leases import (
    LeaseConflict,
    LeaseManager,
    StaleWorker,
    utcnow,
)


@pytest.fixture()
def mgr(tmp_path: Path) -> LeaseManager:
    return LeaseManager(tmp_path / "leases.db")


# ---------------------------------------------------------------- AC1 -------
def test_two_simultaneous_workers_cannot_both_hold_lease_threads(mgr: LeaseManager):
    """AC1: two concurrent claimers -> exactly one valid exclusive lease."""
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def claim(node: str) -> None:
        barrier.wait()
        try:
            lease = mgr.grant("job-1", node, ttl_seconds=300)
            with lock:
                results.append(f"granted:{lease.fencing_token}")
        except LeaseConflict:
            with lock:
                results.append("conflict")

    threads = [threading.Thread(target=claim, args=(f"node-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [r for r in results if r.startswith("granted")]
    assert len(granted) == 1, f"expected exactly one grant, saw {results}"
    assert results.count("conflict") == 7


def _proc_claim(db_path: str, start_at: float, out: "mp.Queue") -> None:  # noqa: F821
    import time

    from hermes_fleet.leases import LeaseConflict, LeaseManager

    m = LeaseManager(db_path)
    while time.time() < start_at:  # busy-wait to maximize the race
        pass
    try:
        lease = m.grant("job-p", "proc", ttl_seconds=300)
        out.put(("granted", lease.fencing_token))
    except LeaseConflict:
        out.put(("conflict", None))
    except Exception as exc:  # pragma: no cover - surfaced as test failure
        out.put(("error", repr(exc)))


@pytest.mark.parametrize("workers", [4])
def test_two_simultaneous_workers_cannot_both_hold_lease_processes(tmp_path: Path, workers: int):
    """AC1 (stronger): separate OS processes racing on the same SQLite file."""
    db_path = str(tmp_path / "leases.db")
    LeaseManager(db_path)  # initialize schema in parent
    ctx = mp.get_context("fork")
    out: "mp.Queue" = ctx.Queue()
    import time

    start_at = time.time() + 0.3
    procs = [ctx.Process(target=_proc_claim, args=(db_path, start_at, out)) for _ in range(workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)

    results = [out.get(timeout=5) for _ in range(workers)]
    granted = [r for r in results if r[0] == "granted"]
    assert not [r for r in results if r[0] == "error"], results
    assert len(granted) == 1, f"expected exactly one grant, saw {results}"


# ---------------------------------------------------------------- AC2 -------
def test_stale_worker_cannot_commit_result_or_side_effect(mgr: LeaseManager):
    """AC2: an owner holding a superseded fence cannot commit protected work."""
    first = mgr.grant("job-2", "node-A", ttl_seconds=1)
    # Force expiry, then reassign to another node -> new, higher fence.
    expired = mgr.expire_due(now=utcnow() + timedelta(seconds=2))
    assert expired == [first.lease_id]
    second = mgr.grant("job-2", "node-B", ttl_seconds=300)

    assert second.fencing_token > first.fencing_token

    with pytest.raises(StaleWorker):
        mgr.commit_side_effect("job-2", first.lease_id, first.fencing_token, action="git-push")
    with pytest.raises(StaleWorker):
        mgr.submit_result(
            "job-2", first.lease_id, first.fencing_token, final_status="completed"
        )

    # The current owner still succeeds.
    event = mgr.submit_result("job-2", second.lease_id, second.fencing_token, final_status="completed")
    assert event["event_type"] == "COMPLETED"


def test_release_then_stale_token_rejected(mgr: LeaseManager):
    lease = mgr.grant("job-rel", "node-A", ttl_seconds=300)
    mgr.release("job-rel", lease.lease_id, lease.fencing_token)
    with pytest.raises(StaleWorker):
        mgr.submit_result("job-rel", lease.lease_id, lease.fencing_token, final_status="completed")
    # A new attempt gets a strictly higher fence.
    nxt = mgr.grant("job-rel", "node-A", ttl_seconds=300)
    assert nxt.fencing_token == lease.fencing_token + 1


# ---------------------------------------------------------------- AC3 -------
def test_coordinator_restart_reconstructs_ownership_and_history(tmp_path: Path):
    """AC3: rebuilding the LeaseManager from the same DB preserves owner/fence."""
    db = tmp_path / "leases.db"
    m1 = LeaseManager(db)
    lease = m1.grant("job-3", "node-A", ttl_seconds=300)
    before = m1.reduce()
    history_len = len(m1.events())

    # Simulate a coordinator restart: brand-new process object, same durable DB.
    m2 = LeaseManager(db)
    after = m2.reduce()
    assert after.jobs == before.jobs
    assert after.state_of("job-3") == "running"
    assert after.jobs["job-3"]["current_fence"] == lease.fencing_token
    assert len(m2.events()) == history_len  # history preserved, no duplication

    # The surviving owner may still renew/commit with its existing fence.
    m2.renew("job-3", lease.lease_id, lease.fencing_token, ttl_seconds=300)


def test_reconstruct_from_ledger_after_hot_table_loss(tmp_path: Path):
    db = tmp_path / "leases.db"
    m = LeaseManager(db)
    lease = m.grant("job-3b", "node-A", ttl_seconds=600, resources=["usb:1-3:nanovna"])
    # Corrupt the hot projection only; the ledger stays authoritative.
    conn = m.connect()
    conn.execute("UPDATE leases SET state='expired'")
    conn.execute("DELETE FROM lease_resources")
    conn.execute("DELETE FROM fence_counters")
    conn.close()

    m.reconstruct_from_ledger()
    restored = m.connect()
    row = restored.execute("SELECT * FROM leases WHERE lease_id=?", (lease.lease_id,)).fetchone()
    holder = restored.execute("SELECT * FROM lease_resources WHERE resource=?", ("usb:1-3:nanovna",)).fetchone()
    fence = restored.execute("SELECT last_token FROM fence_counters WHERE job_id=?", ("job-3b",)).fetchone()
    restored.close()
    assert row["state"] == "active"
    assert holder is not None and holder["lease_id"] == lease.lease_id
    assert fence["last_token"] == lease.fencing_token


def test_restart_does_not_double_execute_active_job(tmp_path: Path):
    db = tmp_path / "leases.db"
    m1 = LeaseManager(db)
    m1.grant("job-3c", "node-A", ttl_seconds=300)
    m2 = LeaseManager(db)
    # A different worker must not be able to also acquire it.
    with pytest.raises(LeaseConflict):
        m2.grant("job-3c", "node-B", ttl_seconds=300)


# ---------------------------------------------------------------- AC4 -------
def test_idempotency_key_replay_is_safe(mgr: LeaseManager):
    """AC4: replaying the same idempotency key returns the original lease/fence."""
    first = mgr.grant("job-4", "node-A", ttl_seconds=300, idempotency_key="idem-xyz")
    replay = mgr.grant("job-4", "node-A", ttl_seconds=300, idempotency_key="idem-xyz")
    assert replay.lease_id == first.lease_id
    assert replay.fencing_token == first.fencing_token
    # No phantom fence bump, and the replay is recorded as an observation.
    events = [e["event_type"] for e in mgr.events()]
    assert events.count("LEASE_GRANTED") == 1
    assert "IDEMPOTENT_REPLAY" in events


def test_duplicate_ledger_event_id_is_noop(mgr: LeaseManager):
    mgr.register_job("job-4b", event_id="evt-fixed")
    mgr.register_job("job-4b", event_id="evt-fixed")  # replay of same event
    created = [e for e in mgr.events() if e["event_type"] == "JOB_CREATED"]
    assert len(created) == 1


def test_resource_exclusivity(mgr: LeaseManager):
    mgr.grant("job-r1", "node-A", ttl_seconds=300, resources=["usb:nanovna"])
    with pytest.raises(LeaseConflict):
        mgr.grant("job-r2", "node-B", ttl_seconds=300, resources=["usb:nanovna"])
    # After expiry the resource is freed.
    mgr.expire_due(now=utcnow() + timedelta(seconds=400))
    ok = mgr.grant("job-r2", "node-B", ttl_seconds=300, resources=["usb:nanovna"])
    assert ok.fencing_token == 1


# ---------------------------------------------------------------- AC5 -------
def test_reducer_is_deterministic_under_replay(mgr: LeaseManager):
    mgr.register_job("job-5a")
    mgr.register_job("job-5b")
    la = mgr.grant("job-5a", "node-A", ttl_seconds=300)
    mgr.submit_result("job-5a", la.lease_id, la.fencing_token, final_status="completed")
    mgr.grant("job-5b", "node-B", ttl_seconds=300)

    first = mgr.reduce()
    # Replaying the identical ledger yields an identical projection.
    for _ in range(3):
        assert mgr.reduce().jobs == first.jobs
    assert first.state_of("job-5a") == "completed"
    assert first.state_of("job-5b") == "running"


def test_git_compatibility_view_reduces_and_matches_directories(tmp_path: Path):
    """AC5: the classic lifecycle directory layout is a reducible view."""
    mgr = LeaseManager(tmp_path / "leases.db")
    mgr.register_job("j-queue")
    mgr.grant("j-run", "node-A", ttl_seconds=300)
    lb = mgr.grant("j-done", "node-A", ttl_seconds=300)
    mgr.submit_result("j-done", lb.lease_id, lb.fencing_token, final_status="completed")
    lc = mgr.grant("j-failed", "node-A", ttl_seconds=300)
    mgr.submit_result("j-failed", lc.lease_id, lc.fencing_token, final_status="failed")
    mgr.register_job("j-blocked")
    mgr.record("BLOCKED", job_id="j-blocked")

    view = mgr.to_git_view()
    assert view["queue"] == ["j-queue"], view
    assert view["running"] == ["j-run"], view
    assert view["completed"] == ["j-done"], view
    assert view["failed"] == ["j-failed"], view
    assert view["blocked"] == ["j-blocked"], view
