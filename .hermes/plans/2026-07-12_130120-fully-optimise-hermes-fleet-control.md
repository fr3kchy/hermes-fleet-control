# Full-System Optimisation Plan — Hermes Fleet Control

**Created:** 2026-07-12 13:01 AEST  
**Workspace:** `/home/parrot/repos/hermes-fleet-control`  
**Mode:** Plan only; no implementation performed  
**Divine timing:** `CAUTION` — Kp 3, baseline regime, Mars hour. Reversible analysis and tests are safe; defer irreversible deletes, credential rotations and broad service replacement until a fresh `APPROVED` verdict or explicit override.

## Goal

Turn the working local tracer bullet into a secure, durable, restart-safe and genuinely distributed personal Hermes control plane without modifying or coupling itself to Hermes internals. Optimise security, reliability, concurrency, networking, observability, performance, tests, operations, developer experience and upgrade/rollback discipline.

## Evidence-backed current state

- Git branch `main`, clean at audit; latest commit `7b81891`.
- FastAPI control plane is live on loopback `127.0.0.1:8876` and reports database health.
- SQLite has 1 node, 1 task and 5 events; database and connector credential file are mode `0600`.
- Four tests pass; one Starlette/httpx deprecation warning remains.
- Live control-plane process is not independently supervised: it is a descendant of `hermes-gateway.service`, with no fleet-specific systemd unit.
- Tailscale system service is enabled, active and connected as `100.78.172.62`; it does not need boot enablement work.
- Hermes Agent is healthy on v0.18.0 but 89 commits behind; config schema is v30 while v33 is current.
- Hermes gateway is active but its cgroup reports ~19.6 GiB because MCP servers and unrelated descendants are grouped beneath it.
- Hermes cron has 33 active jobs. Several inference jobs are failing due to provider drift; two older model jobs have connection errors.
- Current implementation is intentionally local-only, synchronous and SQLite-backed.

## No-mutation policy for execution

1. Back up project SQLite state and Hermes independently before changing either.
2. Never overwrite `~/.hermes/config.yaml`, `.env`, `auth.json`, sessions, skills or cron files directly.
3. Use supported `hermes config`, `hermes backup`, `hermes update`, `hermes cron` and profile commands.
4. Preserve the current local vertical slice until its replacement passes tests and a live dispatch.
5. Make every schema change through numbered migrations with backward checks.
6. Keep the generic scheduler unaware of Hermes CLI details; all Hermes variation stays behind versioned adapters.
7. Bind externally only through Tailscale plus authenticated TLS/mTLS. Never expose raw port 8876 on `0.0.0.0`.
8. Require explicit approval before deleting nodes, rotating credentials, restoring backups, updating Hermes core, or interrupting live jobs.

## Ranked findings

| Severity | Finding | Planned action |
|---|---|---|
| CRITICAL | `HFC_ENROLLMENT_SECRET` currently has a known fallback (`change-me-local-only`) | Refuse startup outside test mode unless a strong secret is supplied from a mode-0600 environment file or systemd credential. |
| CRITICAL | Task dispatch endpoint has no operator authentication or authorisation | Add operator auth, scoped roles, rate limits, request size limits and an approval policy before any execution. |
| HIGH | Fleet server is an unmanaged child of Hermes gateway and will die/restart with the gateway/session | Create a hardened independent user systemd service with restart policy, readiness checks and resource limits. |
| HIGH | Task execution is synchronous inside an async request, blocking the event loop | Split API from durable worker; enqueue transactionally and execute in a bounded subprocess worker. |
| HIGH | Node secret is stored plaintext in SQLite | Store an encrypted credential envelope or use per-node asymmetric identities/mTLS; retain only hashes where verification permits. |
| HIGH | Remote node transport is absent | Add outbound node connector over Tailscale HTTPS/mTLS with reconnect, replay protection and version negotiation. |
| HIGH | No task approval, cancellation, idempotency, leasing or orphan recovery | Implement explicit task state machine and durable leases with recovery after crash/reboot. |
| HIGH | WebSocket and event replay endpoints are unauthenticated | Apply operator/node scopes, cursor validation, backpressure and connection limits. |
| HIGH | Hermes is 89 commits behind and config schema is v30→v33 | Back up, diff release changes, migrate config through supported commands, smoke-test, then update behind a rollback gate. |
| MEDIUM | Gateway cgroup owns MCP servers and the ad-hoc fleet server, reporting ~19.6 GiB | Separate service ownership and account resource usage per component before setting limits. |
| MEDIUM | Cron jobs fail from provider drift and connection errors | Pin intended provider/model per reasoning job; validate one manual run before restoring schedules. |
| MEDIUM | Heartbeat replaces the full capability manifest with partial payloads | Separate immutable identity, declared capabilities and dynamic metrics; use merge/version semantics. |
| MEDIUM | No stale-node/offline reconciliation | Add TTL-based liveness state, grace periods and deterministic offline events without model inference. |
| MEDIUM | No migrations, retention, pagination, indexes or database integrity schedule | Add migration table, indexes, bounded queries, WAL checkpointing, retention and integrity verification. |
| MEDIUM | Only four tests cover the entire system | Expand unit, integration, concurrency, security, restart, migration, adapter and end-to-end suites. |
| MEDIUM | Structured logs/metrics/traces are absent | Add JSON logs, correlation propagation, Prometheus metrics and OpenTelemetry-compatible spans. |
| MEDIUM | Dependency warning and unpinned broad ranges reduce reproducibility | Resolve TestClient compatibility and introduce lockfile/SBOM/vulnerability checks. |

## Target architecture

```text
Operator UI/CLI
    │ authenticated HTTPS over Tailscale
    ▼
Control Plane API ──► SQLite/PostgreSQL repository
    │                     ├─ nodes/capabilities
    │                     ├─ workflows/tasks/attempts
    │                     ├─ approvals/leases
    │                     └─ append-only events/audit
    ▼
Durable dispatcher queue
    │
    ├─ Local worker ──► HermesCliAdapterV1/V2 ──► explicit Hermes profile
    └─ Remote node connectors ◄── mTLS/Tailscale ──► bounded workers

Event outbox ──► WebSocket/SSE/UI
Metrics/logs ──► lightweight event-driven observability
```

## Implementation roadmap

### Phase 0 — Baseline, backup and acceptance contract

1. Capture current git state, database counts, health, fleet, events and real Hermes dispatch output.
2. Create and verify a SQLite backup with `PRAGMA integrity_check` and test restoration into a temporary path.
3. Create a quick Hermes backup with the supported CLI; verify archive listing without restoring.
4. Record performance baselines: health latency, fleet latency, dispatch duration, idle RSS/CPU and concurrent-read behaviour.
5. Define release acceptance criteria and a machine-readable compatibility matrix for Hermes versions/adapters.

**Files likely to change:**
- `docs/BASELINE.md`
- `docs/ACCEPTANCE.md`
- `scripts/verify_backup.py`
- `tests/test_backup_restore.py`

### Phase 1 — Security hardening first

1. Add failing tests proving startup rejects the default enrollment secret in production mode.
2. Introduce a secrets provider abstraction: systemd credentials or mode-0600 environment file for local deployment; no secret values in git/logs.
3. Add operator identity and scoped permissions (`fleet:read`, `nodes:enrol`, `tasks:create`, `tasks:approve`, `admin`).
4. Authenticate REST, WebSocket and event replay consistently.
5. Add rate limiting, body-size limits, replay-resistant timestamps/nonces on signed node messages and constant-time verification.
6. Replace plaintext node-secret storage with encrypted-at-rest envelopes or mTLS node certificates; document rotation/revocation.
7. Add immutable audit records for enrollment, approval, execution, cancellation, rotation and administrative changes.
8. Run dependency audit, secret scan and adversarial endpoint tests.

**Files likely to change:**
- `src/hermes_fleet/config.py`
- `src/hermes_fleet/security.py`
- `src/hermes_fleet/auth.py` (new)
- `src/hermes_fleet/middleware.py` (new)
- `src/hermes_fleet/app.py`
- `src/hermes_fleet/schemas.py`
- `tests/test_auth.py` (new)
- `tests/test_security.py` (new)
- `SECURITY.md`

### Phase 2 — Durable task lifecycle and non-blocking execution

1. Define a typed state machine: `pending → awaiting_approval → queued → leased → running → succeeded|failed|cancelled|timed_out`.
2. Add idempotency keys and transactional creation of task plus outbox event.
3. Implement a durable worker process with leases, heartbeat, bounded concurrency and orphan recovery.
4. Move blocking Hermes subprocess work out of FastAPI request handlers.
5. Add cancellation that terminates only the owned process group, with bounded graceful shutdown then kill.
6. Enforce prompt/output limits, timeout policy, retry classification and explicit no-shell execution.
7. Persist attempts separately from logical tasks.
8. Return `202 Accepted` only after durable queue commit; never show success before worker confirmation.

**Files likely to change:**
- `src/hermes_fleet/tasks.py` (new)
- `src/hermes_fleet/worker.py` (new)
- `src/hermes_fleet/repository.py` (new)
- `src/hermes_fleet/app.py`
- `src/hermes_fleet/db.py`
- `src/hermes_fleet/schemas.py`
- `tests/test_task_lifecycle.py` (new)
- `tests/test_worker_recovery.py` (new)
- `tests/test_concurrency.py` (new)

### Phase 3 — Database correctness and migrations

1. Introduce numbered, reversible migrations and `schema_version` tracking.
2. Normalise nodes, credentials, capability snapshots, heartbeats, tasks, attempts, approvals and events.
3. Add foreign keys, check constraints and indexes for status, node, timestamps and event cursor.
4. Add pagination and retention for tasks/events; bound every list endpoint.
5. Add busy timeout, connection lifecycle management, WAL checkpoint policy and integrity checks.
6. Implement transactional outbox so committed state and streamed events cannot diverge.
7. Define a PostgreSQL adapter only after SQLite stress thresholds are measured; do not migrate prematurely.

**Files likely to change:**
- `src/hermes_fleet/db.py`
- `src/hermes_fleet/repository.py`
- `src/hermes_fleet/migrations/*.sql` (new)
- `tests/test_migrations.py` (new)
- `tests/test_repository.py` (new)

### Phase 4 — Node protocol and Tailscale distribution

1. Version the node protocol and capability schema.
2. Separate static node identity, declared capabilities, software inventory and dynamic telemetry.
3. Implement outbound connector loop with exponential backoff/jitter, sequence numbers, heartbeat TTL and offline reconciliation.
4. Support remote task lease/poll or authenticated WebSocket channel; prefer outbound node connections to avoid inbound firewall weakening.
5. Terminate TLS through a dedicated proxy or use application mTLS; bind only to the host's Tailscale interface, never all interfaces.
6. Add certificate/credential rotation, node revocation and lost-node recovery.
7. Detect Tailscale readiness and report actionable degraded state without continuous inference.
8. Enrol a second real host and prove dispatch/result/event propagation end to end.

**Files likely to change:**
- `src/hermes_fleet/protocol.py` (new)
- `src/hermes_fleet/connector.py`
- `src/hermes_fleet/node_agent.py` (new)
- `deploy/caddy/Caddyfile` or `deploy/nginx/` (new, selected during execution)
- `tests/test_protocol.py` (new)
- `tests/test_remote_connector.py` (new)
- `docs/NODE_ENROLLMENT.md` (new)

### Phase 5 — Hermes adapter compatibility

1. Probe actual Hermes version and supported CLI flags at worker startup.
2. Define typed adapter capability reports instead of assuming all versions support the same entrypoint.
3. Add V1 current adapter and future adapter fallback selection; reject unsupported versions with remediation.
4. Validate profile existence before queueing execution.
5. Capture stdout/stderr safely, classify timeout/provider/config errors and redact secrets.
6. Test against fake binaries plus one real `default` profile smoke test.
7. Keep workflow scheduling generic; adapters receive only a typed execution request.

**Files likely to change:**
- `src/hermes_fleet/hermes_adapter.py`
- `src/hermes_fleet/adapters/base.py` (new)
- `src/hermes_fleet/adapters/cli_v1.py` (new)
- `src/hermes_fleet/adapters/registry.py` (new)
- `tests/test_hermes_adapters.py` (new)
- `docs/HERMES_COMPATIBILITY.md` (new)

### Phase 6 — Workflow orchestration templates become executable

1. Define typed workflow DAG, node, dependency, acceptance and approval schemas.
2. Persist workflow definitions and runs separately.
3. Implement deterministic dependency resolution, bounded fan-out/fan-in and failure policies.
4. Convert the five existing templates into validated YAML fixtures.
5. Add explicit human approval gates to diagnostics repairs and any host mutation.
6. Add resource claims for GPU jobs and guaranteed cleanup/release.
7. Add evidence reviewer/judge acceptance outputs without allowing those roles to self-authorise mutations.

**Files likely to change:**
- `src/hermes_fleet/workflows.py` (new)
- `src/hermes_fleet/scheduler.py` (new; generic, no Hermes imports)
- `workflows/research-syndicate.yaml` (new)
- `workflows/software-build-team.yaml` (new)
- `workflows/device-diagnostics.yaml` (new)
- `workflows/local-gpu-workload.yaml` (new)
- `workflows/bci-development.yaml` (new)
- `tests/test_workflows.py` (new)
- `tests/test_scheduler.py` (new)

### Phase 7 — Observability and lightweight monitoring

1. Add structured JSON logging with correlation, task, node, workflow and attempt IDs.
2. Add Prometheus-compatible metrics for API latency, queue depth, task states, node freshness, worker utilisation and adapter errors.
3. Add OpenTelemetry-compatible spans across API → queue → worker → Hermes → result/event.
4. Add readiness and liveness endpoints that distinguish database, worker, adapter and event-stream health.
5. Add event-driven alerts for stale nodes, queue stalls, repeated task failures and backup failures.
6. Keep monitoring lightweight; do not use LLM inference for ordinary health checks.
7. Add retention and redaction tests for logs and telemetry.

**Files likely to change:**
- `src/hermes_fleet/logging.py` (new)
- `src/hermes_fleet/metrics.py` (new)
- `src/hermes_fleet/tracing.py` (new)
- `src/hermes_fleet/app.py`
- `tests/test_observability.py` (new)
- `docs/OPERATIONS.md` (new)

### Phase 8 — Independent boot-safe service management

1. Create separate user systemd units for API, worker and local node connector.
2. Load secrets through `EnvironmentFile=` or systemd credentials; set file permissions and avoid command-line secrets.
3. Set `Restart=on-failure`, start limits, clean shutdown timeouts, working directory and explicit dependencies on network-online/Tailscale where appropriate.
4. Add systemd hardening (`NoNewPrivileges`, `PrivateTmp`, restricted write paths, capability bounding) compatible with Hermes execution needs.
5. Add health-check watchdog and prove restart after process kill and reboot.
6. Ensure Hermes gateway restart does not terminate fleet services.
7. Record exact install/uninstall/enable/disable/status/log commands.

**Files likely to change:**
- `deploy/systemd/hermes-fleet-api.service` (new)
- `deploy/systemd/hermes-fleet-worker.service` (new)
- `deploy/systemd/hermes-fleet-node.service` (new)
- `scripts/install-services.sh` (new, reversible)
- `scripts/uninstall-services.sh` (new)
- `tests/test_service_files.py` (new)
- `README.md`

### Phase 9 — Operator UI and truthful UX

1. Build a minimal fleet dashboard only after authenticated APIs and task lifecycle stabilise.
2. Show backend-confirmed node freshness, queue state, task attempts, approvals and event timeline.
3. Never display success optimistically; render pending/running/degraded/error states directly from durable backend state.
4. Surface exact remediation for authentication, offline node, unsupported adapter, provider failure and timeout.
5. Add accessibility, responsive layout, reconnect handling and event replay after WebSocket loss.
6. Add browser E2E tests and console-error gate.

**Files likely to change:**
- `ui/` (new frontend workspace)
- `src/hermes_fleet/static/` or separate deploy package
- `tests/e2e/` (new)
- `docs/UI.md` (new)

### Phase 10 — Hermes host optimisation and integration hygiene

1. Create a verified Hermes backup before any migration.
2. Run supported config migration v30→v33, diff resulting config with secrets redacted and run `hermes doctor`.
3. Evaluate 89 upstream commits and update only after project adapter compatibility tests pass.
4. Pin model/provider on inference cron jobs whose global provider drift caused safe skips; manually run each once and verify `last_run_at`/output.
5. Diagnose the two connection-error jobs before retrying.
6. Separate MCP/fleet process ownership from gateway cgroup to make memory accounting actionable.
7. Resolve unavailable tools only if required; do not install optional integrations merely to make doctor output green.
8. Re-run gateway, cron, MCP and real profile dispatch smoke tests.

**Host paths/areas likely to change only through supported tools:**
- `~/.hermes/config.yaml` via `hermes config migrate/set`
- Hermes installation via `hermes update --backup --restart-gateway`
- Cron jobs via `hermes cron`/cron tool
- user systemd units through reviewed deployment files

### Phase 11 — Supply-chain, release and recovery discipline

1. Pin direct and transitive Python dependencies with a reproducible lock.
2. Resolve the Starlette/httpx test warning through a compatible tested version set, not warning suppression.
3. Add lint, type checking, unit/integration/E2E tests, dependency audit, secret scan and SBOM generation.
4. Add release tags, schema compatibility notes and changelog.
5. Build backup rotation and periodic restore drills.
6. Document upgrade order: stop intake → drain queue → backup → migrate → test → restart → smoke → reopen intake.
7. Document rollback order and data-forward compatibility boundaries.

**Files likely to change:**
- `pyproject.toml`
- lockfile selected during execution
- `.github/workflows/ci.yml` or local CI equivalent
- `CHANGELOG.md` (new)
- `docs/RECOVERY.md` (new)
- `Makefile`

## Test and validation matrix

| Area | Required proof |
|---|---|
| Unit | Security, signatures, state transitions, adapters, repositories and scheduling functions pass deterministically. |
| API integration | Auth scopes, enrollment, heartbeat, pagination, approval, cancellation and error remediation responses. |
| Concurrency | Simultaneous reads/writes, queue leases, duplicate idempotency keys and worker contention. |
| Security | Default-secret refusal, replay rejection, rate limits, revoked node denial, log redaction and unauthorised WebSocket denial. |
| Migration | Empty DB, current v0.1 DB, upgrade/rollback boundary and corrupted backup rejection. |
| Restart | API, worker and connector recover independently; running tasks become recoverable rather than stuck. |
| Hermes compatibility | Fake old/new CLI variants plus one real `default` profile returns exact sentinel output. |
| Remote node | Second Tailscale host enrolls, heartbeats, accepts bounded task, returns result and survives reconnect. |
| Workflow | All five templates validate; dependencies, approval gates, fan-in and failed-step policies are deterministic. |
| Performance | Health/fleet/event p95 targets, idle RSS, queue throughput and event-stream backpressure measured against baseline. |
| Operations | Backup integrity, temporary restore, systemd boot/restart, logs and rollback drill. |
| UI | Browser E2E, no console errors, accessibility smoke, truthful state transitions and reconnect replay. |

## Proposed acceptance gates

- No production startup with known/default secrets.
- No execution endpoint usable without authenticated, authorised operator identity.
- No task reported successful before durable worker confirmation.
- API remains responsive during Hermes task execution.
- API, worker and node connector survive independent restart and reboot.
- Existing Hermes config remains recoverable from a verified backup.
- One local and one remote Tailscale node complete signed heartbeat and real Hermes profile dispatch.
- All five workflow templates execute or fail with deterministic, actionable state.
- Every user-visible error includes a remediation path.
- Full test, type, lint, security, migration, backup/restore and live smoke gates pass.

## Execution order

1. Final read-only baseline and fresh Divine Timing check.
2. Verified project and Hermes backups.
3. Security tests and hardening.
4. Durable queue/worker and task state machine.
5. Migrations/repository/outbox.
6. Independent systemd services.
7. Tailscale remote connector and second-node proof.
8. Hermes adapter/version compatibility.
9. Workflow engine/templates.
10. Observability and performance tuning.
11. UI.
12. Hermes config migration/update and cron repair behind rollback gates.
13. Full recovery drill, documentation and staged release.

## Risks and tradeoffs

- SQLite remains the best initial fit for a personal fleet, but write concurrency must be measured before deciding on PostgreSQL.
- mTLS gives stronger node identity than shared HMAC but adds certificate lifecycle complexity; automate issuance, rotation and revocation before adoption.
- Splitting API/worker/connector increases service count but removes the current gateway-coupling and event-loop blocking defects.
- Updating Hermes may alter CLI behaviour; adapter tests and a verified backup must precede it.
- Remote execution expands blast radius. Keep default policies bounded, profile-scoped and approval-gated.
- Current CAUTION timing means irreversible cleanup, credential invalidation and broad upgrades should wait; all implementation should remain reversible regardless.

## Open decisions to resolve during execution

1. Operator authentication: local admin token, Tailscale identity-aware proxy, or OIDC. Preferred evaluation order: Tailscale identity/OIDC, then scoped local token as bootstrap.
2. Node identity: application mTLS versus encrypted HMAC credentials. Preferred target: mTLS, with HMAC retained only for bootstrap migration.
3. UI stack: server-rendered minimal UI versus separate TypeScript SPA. Choose after API schemas stabilise.
4. PostgreSQL trigger threshold: define measured queue/write concurrency limit before migration.
5. First remote node: select the most stable Tailscale host and verify its Hermes profile/tool availability before enrollment.
