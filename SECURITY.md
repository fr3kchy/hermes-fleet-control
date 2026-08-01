# Security Policy

## Supported version

`0.1.x` receives security fixes during foundation development.

## Controls

- Control plane binds to `127.0.0.1` by default.
- Enrollment tokens are random, short-lived, node-bound and single-use.
- Heartbeats require HMAC-SHA256 over canonical JSON.
- Node credentials are written with mode `0600`.
- Hermes tasks use an explicit profile, bounded timeout and argument arrays; no shell interpolation.
- Secrets are excluded from logs, API fleet responses and git.
- Existing `~/.hermes/config.yaml`, `.env`, and `auth.json` are never modified.

## Deployment boundary

Do not expose port 8765 directly to an untrusted network. For multiple hosts, use Tailscale plus HTTPS, or a reverse proxy with mTLS. Rotate a node by deleting its record and enrolling it with a fresh token.

## Reporting

Record the endpoint, correlation ID, timestamp and reproducible request. Do not include credentials.
