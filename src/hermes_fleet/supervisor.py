import asyncio
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .db import Database, now


ACTIVE = ("assigned", "accepted", "running", "review", "verifying")


class FleetSupervisor:
    """Low-cost deterministic health/recovery loop; it never executes tasks."""

    def __init__(self, db: Database, interval_seconds: int = 900, wake_url: str | None = None, wake_key: str | None = None):
        self.db, self.interval_seconds, self.wake_url, self.wake_key = db, max(60, interval_seconds), wake_url, wake_key
        self._stop = asyncio.Event()

    def reconcile_once(self) -> dict:
        stamp, current = now(), datetime.now(timezone.utc)
        stale_nodes, uncertain_tasks, summary_nodes = [], [], []
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id,status,last_seen FROM nodes WHERE status NOT IN ('offline','stale')").fetchall():
                try:
                    age = (current - datetime.fromisoformat(row["last_seen"])).total_seconds() if row["last_seen"] else 999999
                except ValueError:
                    age = 999999
                if age > 90:
                    db.execute("UPDATE nodes SET status='stale' WHERE id=?", (row["id"],))
                    stale_nodes.append(row["id"])
            for row in db.execute("SELECT id,summary_at FROM nodes").fetchall():
                try:
                    summary_age = (current - datetime.fromisoformat(row["summary_at"])).total_seconds() if row["summary_at"] else 999999
                except ValueError:
                    summary_age = 999999
                if summary_age >= 3600:
                    db.execute("UPDATE nodes SET summary_at=? WHERE id=?", (stamp, row["id"]))
                    summary_nodes.append(row["id"])
            stale_set = set(stale_nodes)
            for row in db.execute("SELECT id,node_id,assignment_delivered_at FROM tasks WHERE status='assigned'").fetchall():
                try:
                    age = (current - datetime.fromisoformat(row["assignment_delivered_at"])).total_seconds()
                except (TypeError, ValueError):
                    age = 999999
                if age > 300 and row["node_id"] in stale_set:
                    db.execute("UPDATE tasks SET status='unknown',reconciled_at=?,updated_at=? WHERE id=?", (stamp, stamp, row["id"]))
                    uncertain_tasks.append(row["id"])
            db.execute("UPDATE tasks SET reconciled_at=? WHERE status IN ('accepted','running','review','verifying')", (stamp,))
        for node_id in stale_nodes:
            self.db.event("node.stale", node_id, {"node_id": node_id, "missed_heartbeats": 3})
        for task_id in uncertain_tasks:
            task = self.db.get_task(task_id)
            self.db.event("task.unknown", task["correlation_id"], {"task_id": task_id, "reason_code": "ASSIGNMENT_ACK_TIMEOUT"})
        for node_id in summary_nodes:
            node = self.db.get_node(node_id)
            self.db.event("node.hourly_summary", node_id, {"node_id": node_id, "status": node["status"], "last_seen": node["last_seen"]})
        return {"stale_nodes": stale_nodes, "uncertain_tasks": uncertain_tasks, "summary_nodes": summary_nodes, "reconciled_at": stamp}

    async def wake_origin(self, task: dict, reason: str) -> bool:
        if not self.wake_url or not self.wake_key or not task.get("origin_session_id"):
            return False
        message = (
            f"Automatic fleet notification: task {task['id']} is {task['status']} "
            f"({reason}). Inspect the correlated fleet task and local Kanban evidence; "
            "resume useful work, request approval, or report the verified outcome as appropriate."
        )
        url = self.wake_url.rstrip("/") + f"/api/sessions/{quote(task['origin_session_id'], safe='')}/chat"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, json={"message": message}, headers={"Authorization": f"Bearer {self.wake_key}"})
                return response.is_success
        except httpx.HTTPError:
            return False

    async def run(self) -> None:
        self.reconcile_once()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                self.reconcile_once()

    def stop(self) -> None:
        self._stop.set()
