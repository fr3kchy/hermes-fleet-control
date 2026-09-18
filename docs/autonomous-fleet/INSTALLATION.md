# Profile, skill and plugin installation

Preview only:

```bash
hermes-fleet bootstrap --role coordinator
hermes-fleet bootstrap --role worker
hermes-fleet bootstrap --role operator
```

Apply after reviewing/approving the preview:

```bash
hermes-fleet bootstrap --role coordinator --apply
hermes-fleet validate
```

Bootstrap creates missing profiles with `hermes profile create --no-skills`, merges only the shipped fleet-owned config keys, installs the native plugin and fleet-supervisor skill, and validates Hermes, Kanban, plugin contracts and service health. It never clones `.env`, credentials, memory or browser identities. Existing fleet-owned SOUL/plugin/skill files are refused unless `--replace-owned` is supplied; replacements are backed up.

Chief gets fleet, Kanban, memory/session/skill and clarification surfaces. Dispatcher gets fleet and Kanban only. Reviewer is read-only by default. Operator owns isolated browser/computer-use credentials. Adjust toolset identifiers to those reported by the live Hermes version before applying in production.
