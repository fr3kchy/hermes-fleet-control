import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


class KanbanError(RuntimeError):
    pass


class HermesKanbanAdapter:
    """Deterministic argv-only bridge to node-local Hermes Kanban."""

    def __init__(self, binary: str = "hermes", timeout: int = 30):
        self.binary, self.timeout = binary, timeout

    def create(self, assignment: dict) -> dict:
        title = assignment["objective"].splitlines()[0][:200]
        body = json.dumps({"objective": assignment["objective"], "acceptance_criteria": assignment["acceptance_criteria"], "fleet_task_id": assignment["id"]}, ensure_ascii=False)
        command = [self.binary, "kanban", "create", title, "--body", body, "--assignee", assignment["profile"], "--workspace", assignment.get("payload", {}).get("workspace", "scratch"), "--idempotency-key", f"fleet:{assignment['id']}", "--created-by", "hermes-fleet", "--json"]
        payload = assignment.get("payload", {})
        if payload.get("goal"):
            command.append("--goal")
        if payload.get("max_retries"):
            command.extend(("--max-retries", str(payload["max_retries"])))
        if payload.get("model"):
            command.extend(("--model", payload["model"]))
        if payload.get("provider"):
            command.extend(("--provider", payload["provider"]))
        if int(assignment.get("risk_class") or 0) >= 3:
            command.extend(("--initial-status", "blocked"))
        result = subprocess.run(command, text=True, capture_output=True, timeout=self.timeout, check=False)
        if result.returncode:
            raise KanbanError((result.stderr or result.stdout)[:2000])
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise KanbanError("Hermes Kanban returned invalid JSON") from exc

    def show(self, card_id: str) -> dict:
        result = subprocess.run([self.binary, "kanban", "show", card_id, "--json"], text=True, capture_output=True, timeout=self.timeout, check=False)
        if result.returncode:
            raise KanbanError((result.stderr or result.stdout)[:2000])
        return json.loads(result.stdout)


class AssignmentMap:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS assignments(fleet_task_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,board_id TEXT NOT NULL,card_id TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,last_sequence INTEGER NOT NULL DEFAULT 0,last_status TEXT NOT NULL DEFAULT '')")
            columns = {row[1] for row in db.execute("PRAGMA table_info(assignments)")}
            if "last_status" not in columns:
                db.execute("ALTER TABLE assignments ADD COLUMN last_status TEXT NOT NULL DEFAULT ''")
        path.chmod(0o600)

    def get(self, fleet_task_id: str) -> dict | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM assignments WHERE fleet_task_id=?", (fleet_task_id,)).fetchone()
        return dict(row) if row else None

    def put(self, fleet_task_id: str, idempotency_key: str, board_id: str, card_id: str) -> dict:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR IGNORE INTO assignments(fleet_task_id,idempotency_key,board_id,card_id) VALUES(?,?,?,?)", (fleet_task_id, idempotency_key, board_id, card_id))
        return self.get(fleet_task_id)

    def all(self) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM assignments ORDER BY created_at")]

    def advance(self, fleet_task_id: str) -> int:
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE assignments SET last_sequence=last_sequence+1 WHERE fleet_task_id=?", (fleet_task_id,))
            return int(db.execute("SELECT last_sequence FROM assignments WHERE fleet_task_id=?", (fleet_task_id,)).fetchone()[0])

    def ensure_sequence(self, fleet_task_id: str, sequence: int) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE assignments SET last_sequence=MAX(last_sequence,?) WHERE fleet_task_id=?", (sequence, fleet_task_id))

    def set_status(self, fleet_task_id: str, status: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE assignments SET last_status=? WHERE fleet_task_id=?", (status, fleet_task_id))
