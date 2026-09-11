# Requirement contract and deterministic placement scheduler

Status: implementation of job `20260911T181858Z-implement-requirement-scheduler`
(step 4 of `execution/FOUNDATION-ORDER.md` in `fr3kchy/research`).

Modules:

- `src/hermes_fleet/requirements.py` — the versioned requirements contract, hard
  eligibility filter and deterministic integer soft score.
- `src/hermes_fleet/placement.py` — concurrency-group planning, the
  one-integration-owner contributor contract, and lease-based work-pull.
- `schemas/execution/job-requirements.v1.schema.json` — machine-readable schema
  for both the task requirements and the node capability snapshot.

`hermes-fleet-control` remains the *implementation* repository. `fr3kchy/research`
stays the durable authority for policy/evidence; this module is inert until wired
into the deployed FastAPI control plane, and it does not modify the existing
worker/lifecycle path.

## Two-stage placement

```text
eligible  = HARD_FILTER(requires, trust, policy, fresh capabilities,
                        connectivity, credentials, locality, safety floors)
score(n)  = integer soft score over reliability, locality, power, energy,
            thermal, resource, latency, queue and retry terms
node      = argmax(eligible, tie-break by ascending node_id)
```

Rules that are enforced by construction:

1. **Hard before soft.** `place_job` only ever considers candidates whose
   `hard_filter` verdict is `eligible=True`. A fast, idle, untrusted node loses
   to a slow, trusted one. Every failing constraint is recorded as a distinct
   human-readable rejection reason.
2. **Deterministic.** Soft scores use integer arithmetic only; the candidate
   list, the selection and the evidence JSON are a pure function of
   `(requirements, registry, now)`, independent of registry input order.
3. **State-sensitive.** Battery level, charging state, mains power, thermal
   headroom and data locality all shift the score, so a registry snapshot change
   moves placement. Genuine safety floors (offline, quarantined, unverified
   identity, thermal limit reached, critical battery while unplugged) are HARD
   rejections, not score penalties.
4. **Instrument-only devices are never generic compute workers.** A node whose
   `device_class` is `instrument`/`sensor`/`esp32`/`tdeck`/`microcontroller`
   is rejected for any job with `requires_compute` unless the job explicitly
   sets `allow_instrument_worker: true`.
5. **Evidence retained.** `PlacementDecision.evidence()` keeps every candidate —
   eligible ones with their score breakdown, ineligible ones with their
   rejection reasons — plus the concurrency plan's serialization reasons and the
   work-pull result's candidate evidence.

## Concurrency is opt-in

`plan_concurrency` is serialized by default. A job may be placed in a batch with
another job only when *all* hold:

- every job in the batch declares `concurrency.safe = true`;
- concurrency groups do not exceed `min(max_parallelism)` for any shared group;
- the selected workers are distinct, `identity_verified`, and carry independent
  `workspace_id` values;
- no job is `exclusive` and no job is `APPROVAL`/`PHYSICAL` (those classes always
  serialize).

Any job that cannot satisfy this starts a new single-job batch, and the specific
reason (`default serialized`, `max_parallelism`, `requires serialization`,
`no independent eligible worker ...`) is recorded in `plan.reasons`. Each job
also gets a `resource_key` (`concurrency-group:<group>:slot<N>`) that is passed
to `LeaseManager.grant` as an exclusive resource, so the default `global` group
is serialized fleet-wide at the lease layer, not merely by convention.

## One integration owner per job

`assign_contributors` issues scoped sub-assignments:

- exactly one `integration_owner`;
- each contributor gets `work/<job-id>/<device-id>/<agent-id>/<attempt-id>` and a
  unique workspace identity;
- duplicate worker identities, a contributor reusing the owner's identity, and
  shared workspaces are refused;
- `guard_lifecycle_action` raises `ContributorAuthorityError` when a contributor
  attempts `finalize`/`promote`/`push_main`/`move_lifecycle`/`claim`, while
  permitting `commit_branch`/`push_branch`/`propose_change`/`write_evidence`.

## Lease-governed work-pull

`WorkPullScheduler` is the scheduler side of "idle workers ask for eligible work":

- a worker presents a verified identity, workspace and session;
- the scheduler filters the queued jobs by that worker's `NodeSnapshot`, chooses
  the highest-priority eligible job, and grants a lease carrying a monotonic
  fencing token; the *worker never selects a different global job by itself*;
- duplicate/cloned worker identities are refused before any grant, and a second
  session cannot acquire a job whose lease is active (`LeaseConflict`);
- a stale session (expired and reassigned lease) cannot `heartbeat` or `complete`
  — both raise `StaleWorker` — while the current fence owner can.

## Promise boundary

At-least-once attempt execution with stale-owner exclusion (as in
`docs/LEASES-FENCING.md`), not "exactly-once". Physical/RF and approval gates are
never weakened by eligibility: `APPROVAL`/`PHYSICAL` jobs still serialize and
still require their own human gates.

## Tests

`tests/test_requirement_scheduler.py` (37 tests) maps one test class per
acceptance criterion of the job; `tests/test_requirement_schema.py` (4 tests)
keeps the Python contract and the JSON schema in step.
