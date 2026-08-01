# ADR-0002: Single-use Enrollment and Signed Node Traffic

**Status:** Accepted

## Decision

Issue short-lived, node-bound, single-use enrollment tokens. Return a random per-node secret only once. Require HMAC-SHA256 signatures over canonical JSON for heartbeat traffic.

## Rationale

This is reversible, local-first and does not weaken host networking. It avoids shipping shared global credentials to every node.

## Consequences

- Token hashes, never raw tokens, are durable.
- Node secrets are stored in the local control-plane database, which must be mode `0600`; application-level encryption is a documented future hardening step.
- Multi-host operation requires HTTPS or mTLS before exposing the API beyond loopback.
