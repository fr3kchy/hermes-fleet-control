import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


JSON_COLUMNS = {"capabilities", "metrics", "requirements", "acceptance_criteria", "payload", "evidence", "artifact_refs", "route_explanation", "policy"}


class Database:
    """Fleet correlation store. Node-local Kanban remains execution authority."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def init(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS enrollment_tokens(token_hash TEXT PRIMARY KEY,node_name TEXT NOT NULL,expires_at REAL NOT NULL,used_at TEXT);
            CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,secret TEXT NOT NULL,status TEXT NOT NULL,capabilities TEXT NOT NULL,metrics TEXT NOT NULL,last_seen TEXT,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,node_id TEXT NOT NULL,profile TEXT NOT NULL,prompt TEXT NOT NULL,status TEXT NOT NULL,output TEXT NOT NULL,error TEXT NOT NULL,correlation_id TEXT NOT NULL,adapter_version TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,idempotency_key TEXT,lease_owner TEXT,FOREIGN KEY(node_id) REFERENCES nodes(id));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT NOT NULL,correlation_id TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            if "idempotency_key" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
            if "lease_owner" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN lease_owner TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status,created_at)")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(1,?)", (now(),))
        self._migrate_v2()
        self.path.chmod(0o600)

    def _migrate_v2(self) -> None:
        task_columns = {
            "origin_node_id": "TEXT", "origin_profile": "TEXT", "origin_session_id": "TEXT",
            "parent_fleet_task_id": "TEXT", "objective": "TEXT", "acceptance_criteria": "TEXT",
            "requirements": "TEXT", "risk_class": "INTEGER", "remote_board_id": "TEXT",
            "remote_card_id": "TEXT", "protocol_version": "TEXT", "result_summary": "TEXT",
            "evidence": "TEXT", "artifact_refs": "TEXT", "failure_code": "TEXT",
            "last_remote_sequence": "INTEGER NOT NULL DEFAULT 0", "last_remote_event_id": "TEXT",
            "reconciled_at": "TEXT", "verified_at": "TEXT", "cancel_requested_at": "TEXT",
            "assignment_delivered_at": "TEXT", "payload": "TEXT", "route_explanation": "TEXT"
        }
        node_columns = {"policy": "TEXT", "capability_hash": "TEXT", "summary_at": "TEXT"}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
            if version >= 2:
                return
            existing = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            for name, spec in task_columns.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {spec}")
            existing_nodes = {row[1] for row in db.execute("PRAGMA table_info(nodes)")}
            for name, spec in node_columns.items():
                if name not in existing_nodes:
                    db.execute(f"ALTER TABLE nodes ADD COLUMN {name} {spec}")
            db.execute("UPDATE nodes SET policy=COALESCE(policy,'{}')")
            db.execute("UPDATE tasks SET status=CASE status WHEN 'queued' THEN 'requested' WHEN 'running' THEN 'unknown' WHEN 'succeeded' THEN 'complete' ELSE status END")
            db.execute("UPDATE tasks SET protocol_version=COALESCE(protocol_version,'1.0'),objective=COALESCE(objective,prompt),acceptance_criteria=COALESCE(acceptance_criteria,'[]'),requirements=COALESCE(requirements,'{}'),risk_class=COALESCE(risk_class,1),evidence=COALESCE(evidence,'[]'),artifact_refs=COALESCE(artifact_refs,'[]'),payload=COALESCE(payload,'{}'),route_explanation=COALESCE(route_explanation,'{}')")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS used_nonces(node_id TEXT NOT NULL,nonce TEXT NOT NULL,used_at REAL NOT NULL,PRIMARY KEY(node_id,nonce),FOREIGN KEY(node_id) REFERENCES nodes(id));
            CREATE TABLE IF NOT EXISTS benchmarks(id INTEGER PRIMARY KEY AUTOINCREMENT,node_id TEXT NOT NULL,name TEXT NOT NULL,value REAL NOT NULL,unit TEXT NOT NULL,recorded_at TEXT NOT NULL,opt_in INTEGER NOT NULL DEFAULT 0,FOREIGN KEY(node_id) REFERENCES nodes(id));
            CREATE INDEX IF NOT EXISTS idx_tasks_assignment ON tasks(node_id,status,created_at);
            CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(correlation_id,id);
            CREATE INDEX IF NOT EXISTS idx_benchmarks_node_name ON benchmarks(node_id,name,id DESC);
            """)
            db.execute("INSERT INTO schema_version(version,applied_at) VALUES(2,?)", (now(),))

    def integrity_check(self) -> str:
        with self.connect() as db:
            return str(db.execute("PRAGMA integrity_check").fetchone()[0])

    @staticmethod
    def _decode(row: sqlite3.Row | dict | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        for key in JSON_COLUMNS & result.keys():
            value = result[key]
            if isinstance(value, str):
                try:
                    result[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass
        return result

    def event(self, event_type: str, correlation_id: str, payload: dict[str, Any]) -> dict:
        created = now()
        with self.connect() as db:
            cursor = db.execute("INSERT INTO events(type,correlation_id,payload,created_at) VALUES(?,?,?,?)", (event_type, correlation_id, json.dumps(payload), created))
        return {"id": cursor.lastrowid, "type": event_type, "correlation_id": correlation_id, "payload": payload, "created_at": created}

    def events(self, after: int = 0) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id", (after,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def fleet(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,name,status,capabilities,metrics,last_seen,created_at,policy FROM nodes ORDER BY name").fetchall()
        return [self._decode(row) for row in rows]

    def get_node(self, node_id: str, include_secret: bool = False) -> dict | None:
        fields = "*" if include_secret else "id,name,status,capabilities,metrics,last_seen,created_at,policy"
        with self.connect() as db:
            row = db.execute(f"SELECT {fields} FROM nodes WHERE id=?", (node_id,)).fetchone()
        return self._decode(row)

    def use_nonce(self, node_id: str, nonce: str, used_at: float, expiry: float) -> bool:
        with self.connect() as db:
            db.execute("DELETE FROM used_nonces WHERE used_at<?", (expiry,))
            try:
                db.execute("INSERT INTO used_nonces(node_id,nonce,used_at) VALUES(?,?,?)", (node_id, nonce, used_at))
                return True
            except sqlite3.IntegrityError:
                return False

    def enqueue_task(self, task: dict) -> dict:
        with self.connect() as db:
            if task.get("idempotency_key"):
                existing = db.execute("SELECT * FROM tasks WHERE idempotency_key=?", (task["idempotency_key"],)).fetchone()
                if existing:
                    return self._decode(existing)
            db.execute("INSERT INTO tasks(id,node_id,profile,prompt,status,output,error,correlation_id,adapter_version,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (task["id"], task["node_id"], task["profile"], task["prompt"], "queued", "", "", task["correlation_id"], task["adapter_version"], task["created_at"], task["created_at"], task.get("idempotency_key")))
        return self.get_task(task["id"])

    def create_fleet_task(self, task: dict) -> tuple[dict, bool]:
        with self.connect() as db:
            if task.get("idempotency_key"):
                existing = db.execute("SELECT * FROM tasks WHERE idempotency_key=?", (task["idempotency_key"],)).fetchone()
                if existing:
                    return self._decode(existing), False
            columns = ["id", "node_id", "profile", "prompt", "status", "output", "error", "correlation_id", "adapter_version", "created_at", "updated_at", "idempotency_key", "origin_node_id", "origin_profile", "origin_session_id", "parent_fleet_task_id", "objective", "acceptance_criteria", "requirements", "risk_class", "protocol_version", "payload", "route_explanation"]
            values = [task.get(c) for c in columns]
            for i, column in enumerate(columns):
                if column in JSON_COLUMNS:
                    values[i] = json.dumps(values[i] if values[i] is not None else ([] if column == "acceptance_criteria" else {}))
            db.execute(f"INSERT INTO tasks({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)
        return self.get_task(task["id"]), True

    def get_task(self, task_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._decode(row)

    def list_tasks(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._decode(row) for row in rows]

    def next_assignment(self, node_id: str) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM tasks WHERE node_id=? AND status IN ('requested','assigned') ORDER BY created_at LIMIT 1", (node_id,)).fetchone()
            if not row:
                return None
            stamp = now()
            db.execute("UPDATE tasks SET status='assigned',assignment_delivered_at=?,updated_at=? WHERE id=? AND status IN ('requested','assigned')", (stamp, stamp, row["id"]))
        return self.get_task(row["id"])

    def acknowledge(self, task_id: str, node_id: str, ack: dict) -> dict | None:
        status = {"accepted": "accepted", "duplicate": "accepted", "rejected": "failed"}[ack["disposition"]]
        with self.connect() as db:
            db.execute("UPDATE tasks SET status=?,remote_board_id=COALESCE(?,remote_board_id),remote_card_id=COALESCE(?,remote_card_id),failure_code=?,last_remote_sequence=?,last_remote_event_id=?,updated_at=? WHERE id=? AND node_id=?", (status, ack.get("remote_board_id"), ack.get("remote_card_id"), ack.get("reason_code"), ack["sequence"], ack["event_id"], now(), task_id, node_id))
        return self.get_task(task_id)

    def apply_remote_event(self, node_id: str, event: dict) -> tuple[dict | None, str]:
        mapping = {"created": "accepted", "ready": "accepted", "running": "running", "blocked": "blocked", "approval_required": "blocked", "review": "review", "done": "verifying", "failed": "failed", "cancelled": "cancelled", "unknown": "unknown"}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE id=? AND node_id=?", (event["fleet_task_id"], node_id)).fetchone()
            if not task:
                return None, "not_found"
            if event.get("idempotency_key") != task["idempotency_key"]:
                return self._decode(task), "idempotency_mismatch"
            if event["event_id"] == task["last_remote_event_id"]:
                return self._decode(task), "duplicate"
            if event["sequence"] <= task["last_remote_sequence"]:
                return self._decode(task), "out_of_order"
            evidence = event.get("evidence", [])
            status = mapping[event["event_type"]]
            verified_at = now() if event["event_type"] == "done" and evidence else None
            if verified_at:
                status = "complete"
            db.execute("UPDATE tasks SET status=?,remote_board_id=COALESCE(?,remote_board_id),remote_card_id=COALESCE(?,remote_card_id),result_summary=?,evidence=?,artifact_refs=?,failure_code=?,last_remote_sequence=?,last_remote_event_id=?,verified_at=COALESCE(?,verified_at),updated_at=? WHERE id=?", (status, event.get("remote_board_id"), event.get("remote_card_id"), event.get("result_summary"), json.dumps(evidence), json.dumps(event.get("artifacts", [])), event.get("failure_code"), event["sequence"], event["event_id"], verified_at, now(), event["fleet_task_id"]))
        return self.get_task(event["fleet_task_id"]), "applied"

    def lease_task(self, worker_id: str) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET status='running',lease_owner=?,updated_at=? WHERE id=? AND status='queued'", (worker_id, now(), row["id"]))
        return self.get_task(row["id"])

    def finish_task(self, task_id: str, task_status: str, output: str, error: str) -> dict:
        with self.connect() as db:
            db.execute("UPDATE tasks SET status=?,output=?,error=?,lease_owner=NULL,updated_at=? WHERE id=?", (task_status, output, error, now(), task_id))
        return self.get_task(task_id)
