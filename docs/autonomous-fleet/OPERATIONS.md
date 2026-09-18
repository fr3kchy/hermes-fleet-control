# Operations and recovery

Roll out in three stages: shadow routing, real node dispatch, then keep the legacy worker disabled after the local restart slice succeeds. `/api/v1` remains for migration. Set `HFC_LEGACY_WORKER_MODE=true` only for the one-release direct-worker compatibility lane.

The API reconciles at startup and every 15 minutes. The node connector sends immediate lifecycle updates through the Hermes plugin hook and runs `kanban show --json` every 15 minutes as a safety net. Routine heartbeats update latest state without appending events; transitions, capability changes, summaries and faults are events.

Recovery rules:

1. Reconcile the recorded local card ID.
2. A duplicate assignment uses the persisted mapping and returns `duplicate`.
3. Never create a second card when card creation or an external effect is uncertain.
4. Re-route only proven-absent idempotent work.
5. Treat cancel as pending until local confirmation.
6. Wake the originating Chief session only for resumable work, approval/review, unrecoverable failure or verified parent completion.

Wake-back is opt-in through `HFC_HERMES_WAKE_URL` (the loopback Hermes API-server base URL) and `HFC_HERMES_WAKE_KEY`. Fleet Control posts a bounded automatic notification to Hermes' native `POST /api/sessions/{session_id}/chat` route. The existing bounded-autonomy reconciler keeps its service-health remit; fleet reconciliation does not restart those services.

FR3K is detected by the `hey-fr3k` executable and local repository names but remains disabled. Enable only bounded allowlisted MCP capabilities on specialist profiles. It may not change SOUL files, skills, approvals, secrets, Hermes core or fleet code outside normal review.
