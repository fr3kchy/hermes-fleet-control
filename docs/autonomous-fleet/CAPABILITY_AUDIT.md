# Hermes 0.20.6 capability audit

Source authority: the installed Hermes Agent v0.20.6 tree and CLI on 28 August 2026 (Australia/Brisbane). The installed upstream revision moved during implementation (latest observed `99c3cad8`), so compatibility claims are pinned to the verified 0.20.6 CLI contracts rather than a changing ahead/behind count.

| Behaviour | Classification | Fleet action |
|---|---|---|
| Profiles/Bot identity, SOUL, isolated config/memory/credentials | Native | Reference profile IDs |
| `/goal` persistent turn loop | Native | Do not duplicate |
| Durable task graph, retries, review, attachments, idempotency | Native Kanban | Local execution authority |
| Cron/routines | Native | Do not turn fleet into a scheduler |
| Skills and `/learn` | Native | Install one routing skill; no second skill store |
| Peer and Bot messaging | Native | Conversational coordination only |
| Browser identity and computer use | Native/configuration | Advertise availability; operator owns credentials |
| Native approval transport and `pre_tool_call` | Native | Fleet plugin escalates mutating actions |
| Cross-node deterministic durable assignment | Missing | Protocol v2 + node connector |
| Capability/trust/locality route explanation | Missing | Strict gates + weighted score |
| Cross-node event ordering and recovery cursor | Missing | Fleet correlation envelope |
| Cross-node acceptance evidence and Chief wake-back | Partial | Evidence gate + configured local API wake |
| Automatic trajectory-to-skill induction | Partial/native `/learn` | Deliberately deferred; extend `/learn`, not fleet |

No Hermes core patch was required for the Kanban vertical slice.
