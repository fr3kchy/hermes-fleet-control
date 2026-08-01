# ADR-0001: Separate Generic Control Plane from Hermes Adapter

**Status:** Accepted

## Decision

Keep durable fleet/task/event state, enrollment and scheduling contracts generic. Put Hermes CLI invocation behind a versioned `HermesAdapter` interface.

## Rationale

Hermes CLI capabilities vary by version. A narrow adapter prevents version churn from contaminating scheduler and API code and makes unsupported behaviour explicit.

## Consequences

- The control plane never imports Hermes internals.
- Each adapter reports detected version and structured failures.
- New execution methods can be added without changing generic task schemas.
