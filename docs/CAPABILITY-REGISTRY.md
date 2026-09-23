# Evidence-backed capability registry

The fleet-control service treats capabilities as claims, not facts. Every claim records a trust tier (`self_declared`, `observed`, `probed`, `benchmarked`, or `curator_verified`), proof type, evidence references, verification time, TTL and revocation state. A claim is scheduler-eligible only when it has observed-or-stronger provenance, at least one evidence reference, and is fresh; self-declaration alone cannot elevate trust.

Worker identity is the tuple `device_id:agent_id:instance_id`. `worker_id` is a deterministic hash of that tuple, while `branch_namespace` is `work/<device>/<agent>/<instance>`. Simultaneous agents therefore cannot share a worker or branch namespace. Duplicate live identities are quarantined instead of arbitrarily selected.

Resource kinds are explicit: Linux worker, Android worker, embedded device and instrument. Instruments are represented in the registry for inventory and provenance but never become scheduler workers. Embedded devices likewise require an appropriate supervisor boundary; the registry does not pretend all resources are equivalent.

Claims expire by TTL and revoked claims remain visible for audit but are ineligible. Legacy `NODES.yaml` migration creates self-declared claims with legacy evidence and a zero TTL, preserving history without silently upgrading trust. The routing projection rejects quarantined/ineligible entries before capability scoring.

Linux probing is bounded and non-secret: OS, architecture, kernel, Python and tool presence. Probe output must be stored with its evidence reference and freshness metadata; it is not a replacement for curator verification.

Implementation: `src/hermes_fleet/capability_registry.py`, with routing integration in `src/hermes_fleet/routing.py` and regression tests in `tests/test_capability_registry.py`.
