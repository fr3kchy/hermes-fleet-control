# Operations and recovery

## Runtime ownership

`hermes-fleet-api`, `hermes-fleet-worker`, and `hermes-fleet-node` are independent user services. They are not children of `hermes-gateway.service`.

## Routine checks

```bash
systemctl --user is-active hermes-fleet-{api,worker,node}.service
curl -fsS http://127.0.0.1:8876/healthz
sqlite3 data/fleet.db 'PRAGMA integrity_check;'
journalctl --user -u hermes-fleet-api.service -u hermes-fleet-worker.service -u hermes-fleet-node.service -n 100 --no-pager
```

## Recovery order

1. Stop node intake and worker.
2. Back up the current database even if degraded.
3. Verify the selected backup with `PRAGMA integrity_check`.
4. Stop the API and replace the database.
5. Set mode `0600`.
6. Start API, then worker, then node connector.
7. Run health, fleet and a bounded real profile task.

Never restore Hermes and fleet state as one implicit operation.
