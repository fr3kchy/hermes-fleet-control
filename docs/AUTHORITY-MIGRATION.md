# Fleet authority migration

Status: implemented in the `hermes-fleet-control` mainline; `hermes-autonomous-fleet` is a protocol-v2 source/archive and must not run as a second scheduler.

## Authority contract

- `fr3kdev/research` remains the durable FR3K policy, evidence, lifecycle and promotion authority.
- `hermes-fleet-control` is the sole supported live scheduler, lease/correlation store and routing authority.
- Node-local Hermes Kanban remains the execution surface for a worker; it is not a global scheduler.
- `hermes-autonomous-fleet` contributes protocol-v2 schemas, deterministic routing, signed request handling, idempotency and reconciliation behavior. Its supervisor/legacy worker path is deprecated for live deployment.
- Android/Linux clients, transports, embedded gateways and UI components are clients or scoped workers. They cannot claim or finalize FR3K lifecycle state.

## Integrated capabilities

The v2 integration provides protocol-bounded task creation, requirement/trust/locality routing with rejection explanations, node-bound signed assignment polling, nonce replay protection, idempotent acknowledgements, ordered lifecycle events, evidence-gated completion, and periodic stale-node/task reconciliation.

## Duplicate scheduler guard

Production service installation must start only the fleet-control API and explicitly disable legacy autonomous worker services. A deployment that enables both repositories as schedulers is unsupported and violates the single-authority invariant. The `hermes-fleet-control` service remains the only component permitted to issue global assignments.

## Worker identity and assignment identity

A node identity is the enrolled node record. An assignment identity is the immutable fleet task UUID plus its idempotency key and ordered remote event sequence. Every worker request is bound to the enrolled node, request path, timestamp, nonce and HMAC; duplicate nonces are rejected. A duplicate request with the same idempotency key returns the existing task rather than creating a second assignment. Remote lifecycle events must match node, task, idempotency key and strictly increasing sequence before they can update state.

These controls prevent duplicate connections from the same node/agent from acquiring the same assignment accidentally while preserving evidence for rejected/out-of-order attempts.

## Verification boundary

The fleet service only records a task as `complete` when a `done` event includes evidence. It does not promote repository lifecycle state, bypass approval/physical gates, or infer success from process liveness. Canonical FR3K promotion remains owned by `gpd-win-mini` through its executor and isolated worker branches.
