"""Failure-injection suite for the lease/fencing/event-ledger protocol.

Every scenario records a machine-readable result; the module-scoped autouse
fixture writes the whole run to ``docs/evidence/failure-injection-report.json``
so the evidence is retained alongside the code.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from hermes_fleet.leases import (
    LeaseConflict,
    LeaseError,
    LeaseManager,
    StaleWorker,
    utcnow,
)

_EVIDENCE_PATH = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "failure-injection-report.json"
_RESULTS: list[dict] = []


def _record(scenario: str, ok: bool, detail: dict) -> None:
    _RESULTS.append({"scenario": scenario, "passed": bool(ok), "detail": detail})


@pytest.fixture(scope="module", autouse=True)
def _retain_evidence():
    yield
    _EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _EVIDENCE_PATH.write_text(
        json.dumps(
            {
                "suite": "failure-injection",
                "generated_at": utcnow().isoformat(),
                "scenarios": _RESULTS,
                "all_passed": all(s["passed"] for s in _RESULTS),
            },
            indent=2,
        )
    )


@pytest.fixture()
def mgr(tmp_path: Path) -> LeaseManager:
    return LeaseManager(tmp_path / "leases.db")


# --- FI-1: crash mid-grant must not leave partial state ---------------------
def test_crash_during_grant_rolls_back_atomically(mgr: LeaseManager, monkeypatch):
    """A crash after fence/resource writes but before commit must roll back."""
    original = LeaseManager.append_event

    def boom(self, db, event_type, **kwargs):
        if event_type == "LEASE_GRANTED":
            raise RuntimeError("injected crash before commit")
        return original(self, db, event_type, **kwargs)

    monkeypatch.setattr(LeaseManager, "append_event", boom)
    with pytest.raises(RuntimeError):
        mgr.grant("job-x", "node-A", ttl_seconds=300, resources=["res:1"])
    monkeypatch.undo()

    # No lease, no fence advance, no resource lock -> the next genuine attempt wins fence 1.
    db = mgr.connect()
    leases = db.execute("SELECT COUNT(*) c FROM leases").fetchone()["c"]
    fence = db.execute("SELECT COUNT(*) c FROM fence_counters").fetchone()["c"]
    db.close()
    ok = mgr.grant("job-x", "node-A", ttl_seconds=300, resources=["res:1"])
    _record(
        "crash_mid_grant_rollback",
        leases == 0 and fence == 0 and ok.fencing_token == 1,
        {"leases_after_crash": leases, "fence_rows_after_crash": fence, "next_fence": ok.fencing_token},
    )
    assert leases == 0 and fence == 0 and ok.fencing_token == 1


# --- FI-2: write-lock contention fails closed (network/partition analogue) --
def test_lock_contention_fails_closed(tmp_path: Path):
    db = tmp_path / "leases.db"
    mgr = LeaseManager(db, busy_timeout_ms=200)
    holder = mgr.connect()
    holder.execute("BEGIN IMMEDIATE")  # hold the write lock to simulate a partition

    failed_closed = False
    try:
        mgr.grant("job-lock", "node-A", ttl_seconds=300)
    except sqlite3.OperationalError:
        failed_closed = True
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    dbh = mgr.connect()
    leases = dbh.execute("SELECT COUNT(*) c FROM leases").fetchone()["c"]
    dbh.close()
    # After the lock is released the job is still claimable at fence 1.
    got = mgr.grant("job-lock", "node-A", ttl_seconds=300)
    _record(
        "lock_contention_fails_closed",
        failed_closed and leases == 0 and got.fencing_token == 1,
        {"failed_closed": failed_closed, "leases_during_lock": leases, "later_fence": got.fencing_token},
    )
    assert failed_closed and leases == 0 and got.fencing_token == 1


# --- FI-3: stale worker returns after reassignment --------------------------
def test_stale_worker_returning_after_reassignment(mgr: LeaseManager):
    a = mgr.grant("job-reassigned", "node-A", ttl_seconds=1)
    mgr.expire_due(now=utcnow() + timedelta(seconds=2))
    b = mgr.grant("job-reassigned", "node-B", ttl_seconds=300)
    rejected_side, rejected_result = False, False
    try:
        mgr.commit_side_effect("job-reassigned", a.lease_id, a.fencing_token, action="flash")
    except StaleWorker:
        rejected_side = True
    try:
        mgr.submit_result("job-reassigned", a.lease_id, a.fencing_token, final_status="completed")
    except StaleWorker:
        rejected_result = True
    mgr.submit_result("job-reassigned", b.lease_id, b.fencing_token, final_status="completed")
    _record(
        "stale_worker_after_reassignment",
        rejected_side and rejected_result and b.fencing_token == a.fencing_token + 1,
        {"side_effect_rejected": rejected_side, "result_rejected": rejected_result,
         "fence_a": a.fencing_token, "fence_b": b.fencing_token},
    )
    assert rejected_side and rejected_result and b.fencing_token == a.fencing_token + 1


# --- FI-4: duplicate result submission (replay of terminal boundary) --------
def test_duplicate_result_submission_is_rejected(mgr: LeaseManager):
    lease = mgr.grant("job-dup", "node-A", ttl_seconds=300)
    mgr.submit_result("job-dup", lease.lease_id, lease.fencing_token, final_status="completed")
    second_rejected = False
    try:
        mgr.submit_result("job-dup", lease.lease_id, lease.fencing_token, final_status="completed")
    except StaleWorker:
        second_rejected = True
    completed = [e for e in mgr.events() if e["event_type"] == "COMPLETED"]
    _record(
        "duplicate_result_rejected",
        second_rejected and len(completed) == 1,
        {"second_rejected": second_rejected, "completed_events": len(completed)},
    )
    assert second_rejected and len(completed) == 1


# --- FI-5: expiry storm is idempotent --------------------------------------
def test_expiry_storm_idempotent(tmp_path: Path):
    mgr = LeaseManager(tmp_path / "leases.db")
    for i in range(20):
        mgr.grant(f"job-e{i}", "node-A", ttl_seconds=1)
    first = mgr.expire_due(now=utcnow() + timedelta(seconds=2))
    second = mgr.expire_due(now=utcnow() + timedelta(seconds=2))
    expired_events = [e for e in mgr.events() if e["event_type"] == "LEASE_EXPIRED"]
    _record(
        "expiry_storm_idempotent",
        len(first) == 20 and second == [] and len(expired_events) == 20,
        {"first_pass": len(first), "second_pass": len(second), "expired_events": len(expired_events)},
    )
    assert len(first) == 20 and second == [] and len(expired_events) == 20


# --- FI-6: ledger gap is detected, not guessed through ----------------------
def test_ledger_gap_detected(mgr: LeaseManager):
    mgr.register_job("g1")
    mgr.register_job("g2")
    mgr.register_job("g3")
    db = mgr.connect()
    db.execute("DELETE FROM ledger WHERE event_type='JOB_CREATED' AND job_id='g2'")
    db.close()
    gaps = mgr.ledger_gaps()
    _record("ledger_gap_detected", gaps == [2], {"gaps": gaps})
    assert gaps == [2]


# --- FI-7: renew with a superseded token is rejected -----------------------
def test_renew_with_superseded_token_rejected(mgr: LeaseManager):
    a = mgr.grant("job-rn", "node-A", ttl_seconds=1)
    mgr.expire_due(now=utcnow() + timedelta(seconds=2))
    b = mgr.grant("job-rn", "node-B", ttl_seconds=300)
    rejected = False
    try:
        mgr.renew("job-rn", a.lease_id, a.fencing_token, ttl_seconds=300)
    except StaleWorker:
        rejected = True
    renewed = mgr.renew("job-rn", b.lease_id, b.fencing_token, ttl_seconds=300)
    _record(
        "renew_superseded_rejected",
        rejected and renewed.fencing_token == b.fencing_token,
        {"old_renew_rejected": rejected, "new_fence_stable": renewed.fencing_token == b.fencing_token},
    )
    assert rejected and renewed.fencing_token == b.fencing_token


# --- FI-8: unsupported event / status fail closed --------------------------
def test_invalid_inputs_fail_closed(mgr: LeaseManager):
    lease = mgr.grant("job-inv", "node-A", ttl_seconds=300)
    bad_status, bad_event = False, False
    try:
        mgr.submit_result("job-inv", lease.lease_id, lease.fencing_token, final_status="ok")
    except LeaseError:
        bad_status = True
    try:
        mgr.record("NOT_A_REAL_EVENT", job_id="job-inv")
    except LeaseError:
        bad_event = True
    _record("invalid_inputs_fail_closed", bad_status and bad_event,
            {"bad_status": bad_status, "bad_event": bad_event})
    assert bad_status and bad_event
