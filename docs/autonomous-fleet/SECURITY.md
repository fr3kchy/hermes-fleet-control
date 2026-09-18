# Threat model and approval boundary

Protected assets are node credentials, browser/OAuth sessions, operator policy, local workspaces, user communications, external commitments and evidence integrity. Threats include forged/replayed node traffic, prompt injection, malicious task payloads, command injection, self-asserted trust, approval spoofing, duplicate side effects, recursive delegation and capacity exhaustion.

Controls:

- TLS/VPN plus method/path/timestamp/nonce/body HMAC; coordinator responses are signed.
- Mode-0600 SQLite and node-state files; secrets are not placed in events, task payloads, skills or profiles.
- `subprocess.run([...])` argv creation; no task content is evaluated by a shell.
- Operator-owned policy fields are stripped from capability heartbeats.
- Payload size and credential-key rejection, idempotency, event ordering and bounded concurrency/depth.
- Uncertain mutations remain `unknown`; non-idempotent work is verified before retry.
- R0 reads automatic; R1 reversible internal writes audited; R2 external communication draft-first; R3 commitments, R4 financial/legal and R5 security require approval; R6 destructive also requires exact target verification.
- R3–R6 local cards begin blocked. Approval resumes the same card through Hermes native transport; comments/events cannot spoof approval.

The shared HMAC secret remains in control-plane and node mode-0600 state for this release. Rotation/encrypted-at-rest secret storage is a separate migration. Do not expose port 8876 directly to an untrusted network.
