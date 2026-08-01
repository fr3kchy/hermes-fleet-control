import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
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
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            if "idempotency_key" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
            if "lease_owner" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN lease_owner TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status,created_at)")
            db.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(1,?)", (now(),))
        self.path.chmod(0o600)

    def integrity_check(self) -> str:
        with self.connect() as db:
            return str(db.execute("PRAGMA integrity_check").fetchone()[0])

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
            rows = db.execute("SELECT id,name,status,capabilities,metrics,last_seen,created_at FROM nodes ORDER BY name").fetchall()
        return [{**dict(row), "capabilities": json.loads(row["capabilities"]), "metrics": json.loads(row["metrics"])} for row in rows]

    def enqueue_task(self, task: dict) -> dict:
        with self.connect() as db:
            if task.get("idempotency_key"):
                existing = db.execute("SELECT * FROM tasks WHERE idempotency_key=?", (task["idempotency_key"],)).fetchone()
                if existing:
                    return dict(existing)
            db.execute("INSERT INTO tasks(id,node_id,profile,prompt,status,output,error,correlation_id,adapter_version,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (task["id"], task["node_id"], task["profile"], task["prompt"], "queued", "", "", task["correlation_id"], task["adapter_version"], task["created_at"], task["created_at"], task.get("idempotency_key")))
        return self.get_task(task["id"])

    def get_task(self, task_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

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
