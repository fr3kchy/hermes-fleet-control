# Protocol v2

Operator endpoints use the existing bearer token. Node endpoints sign this canonical byte sequence with the enrolled node HMAC secret:

```text
HTTP METHOD
request path (without query)
unix timestamp
nonce
SHA-256(exact body bytes)
```

Requests older than 90 seconds and reused `(node, nonce)` pairs are rejected. Coordinator responses use the same construction with method `RESPONSE` and `X-Coordinator-*` headers. Use TLS over Tailscale or another approved VPN across hosts; HMAC does not encrypt traffic.

Endpoints:

- `POST /api/v2/tasks`
- `GET /api/v2/tasks/{id}` and `/route`
- `GET /api/v2/nodes/{id}/assignments/next?wait=20`
- `POST /api/v2/nodes/{id}/assignments/{task}/ack`
- `POST /api/v2/nodes/{id}/events`
- `POST /api/v2/tasks/{id}/cancel`
- `POST /api/v2/tasks/{id}/reconcile`
- read-only fleet, node, health and benchmark endpoints

Assignment and event envelopes carry protocol version, UUID task/event identifiers, global idempotency key, origin/destination, timestamp, sequence, objective, acceptance criteria, requirements, risk, and a bounded payload. Credential-like payload keys and payloads over 32 KiB are rejected.

Events are ordered per task. Duplicate event IDs are acknowledged as duplicates; older sequences are rejected. Detailed worker output stays local. Fleet summaries and evidence/artifact references are bounded.
