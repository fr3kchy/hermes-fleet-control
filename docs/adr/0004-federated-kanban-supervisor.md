# ADR 0004: Federated Kanban supervisor supersedes direct workers

Status: accepted

Hermes Fleet Control will be a correlation and routing envelope. Each destination's Hermes Kanban is authoritative for execution. `/goal`, cron, profiles, peers, memory, skills and native approvals retain their Hermes meanings.

The v1 direct worker conflated fleet delivery with agent execution and could not preserve local Kanban review, retry and evidence state. Protocol v2 therefore delivers signed assignments to deterministic node connectors, which create idempotent local cards and return ordered lifecycle evidence.

The existing fleet SQLite database is migrated in place. It stores remote identifiers and recovery cursors, not detailed task plans, worker runs, comments or attachments. `/api/v1` and an explicitly enabled legacy worker remain for one release; new installation leaves the worker disabled.

Consequences: nodes remain useful when the coordinator is unavailable, replay and uncertain side effects have explicit semantics, and normal dispatch costs no LLM turn. Cross-node operation still requires TLS/VPN and enrolled shared HMAC credentials.
