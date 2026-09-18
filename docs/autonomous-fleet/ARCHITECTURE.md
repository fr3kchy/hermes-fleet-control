# Autonomous fleet architecture

Hermes Fleet Control is a thin federated supervisor, not an agent framework or second task engine.

```text
Chief session (/goal when requested)
  -> fleet task correlation envelope
  -> deterministic capability route
  -> signed long-poll assignment
  -> destination Hermes Kanban card
  -> Hermes Kanban worker/reviewer
  -> ordered lifecycle event + evidence
  -> fleet reconciliation
  -> authenticated wake of the originating session
```

Hermes profiles remain Bot identities. Hermes peers remain the conversational channel. Cron remains time-oriented scheduling. `/goal` remains a persistent conversation loop. Node-local Kanban owns task plans, dependencies, comments, attempts, review, approvals and attachments. Fleet SQLite owns only node health/capabilities, routing explanation, assignment correlation, remote card identifiers, ordered event cursors, result/evidence references and recovery timestamps.

Normal dispatch is deterministic and invokes `hermes kanban create` as argv, with `fleet:<fleet-task-id>` as the local idempotency key. The dispatcher profile is for exceptions and inspection only.

## State outline

Fleet task states are `requested`, `assigned`, `accepted`, `running`, `blocked`, `review`, `verifying`, `complete`, `failed`, `cancelled`, and `unknown`. `done` from a node becomes `verifying` without evidence and `complete` only with acceptance evidence. Cancellation remains a request until the local board confirms it.

Startup and 15-minute reconciliation are deterministic. A node becomes stale after three missed 30-second heartbeats. A delivered assignment with no acknowledgement becomes `unknown`; it is not silently recreated.
