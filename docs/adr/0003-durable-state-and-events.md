# ADR-0003: SQLite Durable State and Append-only Events

**Status:** Accepted

## Decision

Use SQLite in WAL mode for nodes, enrollment tokens, tasks and append-only events. Stream committed events over WebSocket.

## Rationale

SQLite gives transactional durability and simple backup/rollback for a personal fleet without adding an external control-plane dependency.

## Consequences

- UI state is derived only from committed backend state.
- WebSocket events carry correlation IDs and database event IDs.
- PostgreSQL migration remains possible through the repository boundary when scale justifies it.
