# Transactional leases, fencing tokens and the runtime event ledger

Status: **implemented** (`src/hermes_fleet/leases.py`, tests in
`tests/test_leases.py`, failure injection in `tests/test_failure_injection.py`).

This document defines the concurrency contract that replaces "who moved the Git
directory first" as FR3K's ownership primitive.

## Motivation

The 2026-09-11 P0 incident proved that Git index locking is not a semantic
execution lease: a loop that batch-claimed six jobs by moving files created
duplicate/split ownership. Directory placement must not be the state machine for
concurrent execution.

## Objects

| Object | Meaning |
|---|---|
| `job_id` | immutable logical job identity |
| `attempt_id` | one execution attempt of a job |
| `lease_id` | one grant of exclusive ownership |
| `fencing_token` | monotonic per-job integer, incremented on every ownership change |
| event ledger | append-only, reducible record of every lifecycle transition |

## Lifecycle events

```
JOB_CREATED -> JOB_ELIGIBLE
  -> LEASE_GRANTED(lease_id, attempt_id, fencing_token)
  -> LEASE_RENEWED*            (never changes the fence)
  -> ATTEMPT_EVENT*
  -> SIDE_EFFECT_COMMITTED     (fence-checked)
  -> RESULT_SUBMITTED -> RESULT_VERIFIED | RESULT_REJECTED
  -> COMPLETED | FAILED | BLOCKED | CANCELLED | SUPERSEDED
LEASE_EXPIRED -> (fresh LEASE_GRANTED with a higher fence)
IDEMPOTENT_REPLAY            (observation only; no ownership change)
```

No event is edited in place. Current state is a deterministic projection
(`LeaseManager.reduce()`); the classic `queue/running/completed/...` layout is a
*materialized compatibility view* (`LeaseManager.to_git_view()`).

## Grant protocol (atomic compare-and-set)

`LeaseManager.grant()` runs inside a single `BEGIN IMMEDIATE` transaction:

1. If an `idempotency_key` was seen before, return the original lease **without**
   allocating a new fence (replay-safe) and append `IDEMPOTENT_REPLAY`.
2. Reject if another **active** lease owns the job (unless the existing lease has
   expired, in which case it is expired and freed).
3. Reject if any requested exclusive resource is held by another active lease.
4. Increment the per-job fence counter (never reused, strictly monotonic).
5. Insert the lease and resource locks; append `LEASE_GRANTED`; commit.

Two simultaneous claimers therefore cannot both win: SQLite serializes writers,
and the unique partial index `idx_lease_one_active_per_job` is a second,
structural guard.

## Protected boundaries

`commit_side_effect()` and `submit_result()` require the caller to present the
**current** `fencing_token`. A worker whose lease was reassigned, expired or
released is rejected with `StaleWorker` — it may still return diagnostic
evidence, but it cannot commit an authoritative side effect or terminal result.

## Renewal, expiry and recovery

- `renew()` extends an active lease and never changes the fence.
- `expire_due()` expires overdue leases, frees their resources and appends
  `LEASE_EXPIRED`; a new attempt receives a strictly higher fence.
- `reduce()` reconstructs current ownership/history from the ledger after a
  coordinator restart, without duplicate execution (an active lease stays
  active and blocks re-grant).
- `reconstruct_from_ledger()` rebuilds the hot projection purely from the
  append-only ledger if it is lost or suspect.
- `ledger_gaps()` detects a truncated ledger so a reducer fails loudly instead
  of guessing across a hole.

## Promise boundary

This is **at-least-once attempt execution with stale-owner exclusion**. It does
not promise exactly-once delivery. Protected side effects are made
*effectively-once* by combining leases + monotonic fencing + idempotency keys.

## Verification

`tests/test_leases.py` maps 1:1 to the job's acceptance criteria (simultaneous
claim via threads and OS processes, stale-worker rejection, coordinator restart,
idempotency replay, Git-view reducer). `tests/test_failure_injection.py`
injects crashes, lock contention, reassignment, duplicate submission, expiry
storms, ledger gaps and invalid input, and retains a machine-readable report at
`docs/evidence/failure-injection-report.json`.
