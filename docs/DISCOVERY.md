# Host and Hermes Discovery Report

Captured: 2026-07-12 AEST. Secret values are intentionally omitted.

## Host

- Hostname: `parrot`
- OS/kernel: Parrot Linux, `6.19.13+parrot7-amd64`, x86_64
- Python: 3.11.15
- CPU: 8 logical CPUs
- RAM: 31 GiB; approximately 25 GiB available during discovery
- Root/home filesystem: 1.9 TiB, 886 GiB free
- GPU: NVIDIA GeForce RTX 3060 12 GiB, driver 595.71.05
- Docker: 26.1.5; current containers include Home Assistant and two healthy PostgreSQL services

## Hermes

- Binary: `/home/parrot/.local/bin/hermes`
- Version: 0.18.0 (2026.7.1), upstream commit `1c4cc00f`
- Source: `/home/parrot/.hermes/hermes-agent`
- Active profile: `default`
- Model/provider: `gpt-5.6-sol` via OpenAI Codex OAuth
- Gateway: active under user systemd
- Telegram: configured
- Cron: 33 active jobs
- Hermes doctor: core environment healthy; config schema is v30 while v33 is available
- Update state: 89 commits behind upstream at discovery time

## Existing services and constraints

- `hermes-gateway.service`, `paperclip-server.service`, and `paperclip-gateway.service` are active.
- Hermes and Ollama bind to loopback; SSH binds to the host network.
- Existing Hermes config, auth and secrets are not modified or copied by this project.
- Port `8876` is selected for this control plane because it was free at discovery time.

## Adapter decision

The verified current Hermes programmatic entrypoint is:

```bash
hermes --profile <profile> -z "<prompt>"
```

The adapter probes `hermes --version`, invokes the profile explicitly, enforces a timeout, captures stdout/stderr separately, and persists the confirmed result. No internal or invented Hermes API is used.

## Known limitations

- The first vertical slice runs a local node connector. Remote transport is deliberately not claimed.
- Enrollment uses short-lived, single-use tokens and per-node HMAC secrets. Production multi-host deployment should terminate TLS at a trusted reverse proxy or add mTLS.
- Task execution is synchronous and bounded in this slice; a durable worker queue is the next scaling step.
