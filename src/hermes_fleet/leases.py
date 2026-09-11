"""Transactional leases, monotonic fencing tokens and an append-only event ledger.

This module replaces "who moved the Git directory first" as a concurrency
primitive with an explicit, DB-transactional ownership protocol:

* exactly one *active* lease may exist per job (and per exclusive resource);
* every ownership change increments a per-job monotonic **fencing token**;
* side-effect and terminal-result commits must present the *current* token,
  so a stale worker whose lease has been reassigned is rejected;
* leases expire; an expired lease can be re-granted with a strictly higher
  fence, and the old holder becomes non-authoritative;
* an append-only, reducible **event ledger** is the lifecycle source of truth.
  Current state is a deterministic projection (reducer) of the ledger, and the
  classic queue/running/completed directory layout is a *materialized
  compatibility view* of that projection.

Promise boundary: this is at-least-once attempt execution with stale-owner
exclusion. It does NOT provide "exactly-once" delivery; it provides
effectively-once *protected* side effects via leases + idempotency + fencing.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

LEASE_ACTIVE = "active"
LEASE_RELEASED = "released"
LEASE_EXPIRED = "expired"
LEASE_COMPLETED = "completed"
LEASE_FAILED = "failed"

# ---------------------------------------------------------------- events ----
# Immutable lifecycle events. No event is ever edited in place.
EVENT_TYPES = (
    "JOB_CREATED",
    "JOB_ELIGIBLE",
    "LEASE_GRANTED",
    "LEASE_RENEWED",
    "LEASE_EXPIRED",
    "LEASE_RELEASED",
    "ATTEMPT_EVENT",
    "SIDE_EFFECT_COMMITTED",
    "RESULT_SUBMITTED",
    "RESULT_VERIFIED",
    "RESULT_REJECTED",
    "COMPLETED",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "SUPERSEDED",
    "IDEMPOTENT_REPLAY",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class LeaseError(Exception):
    """Base class for lease/fencing protocol violations."""


class LeaseConflict(LeaseError):
    """A conflicting active lease (job or resource) already exists."""


class StaleWorker(LeaseError):
    """Caller presented a superseded/expired/unknown lease or fence token."""


@dataclass(frozen=True)
class Lease:
    lease_id: str
    job_id: str
    attempt_id: str
    node_id: str
    fencing_token: int
    resources: tuple[str, ...]
    execution_class: str
    state: str
    issued_at: str
    expires_at: str
    idempotency_key: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Lease":
        return cls(
            lease_id=row["lease_id"],
            job_id=row["job_id"],
            attempt_id=row["attempt_id"],
            node_id=row["node_id"],
            fencing_token=row["fencing_token"],
            resources=tuple(json.loads(row["resources"])),
            execution_class=row["execution_class"],
            state=row["state"],
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            idempotency_key=row["idempotency_key"],
        )

    def expired(self, now: datetime | None = None) -> bool:
        return datetime.fromisoformat(self.expires_at) <= (now or utcnow())

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["resources"] = list(self.resources)
        return d


@dataclass
class ReducedState:
    """Deterministic projection of the event ledger."""

    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def state_of(self, job_id: str) -> str | None:
        job = self.jobs.get(job_id)
        return job["state"] if job else None


class LeaseManager:
    """SQLite-WAL lease authority. One logical instance owns one database file."""

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._init()

    # -- plumbing -----------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        # DDL runs in autocommit mode: executescript() would otherwise commit the
        # explicit BEGIN IMMEDIATE used for DML and break transaction handling.
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS leases(
                  lease_id TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL,
                  attempt_id TEXT NOT NULL,
                  node_id TEXT NOT NULL,
                  fencing_token INTEGER NOT NULL,
                  resources TEXT NOT NULL,
                  execution_class TEXT NOT NULL,
                  state TEXT NOT NULL,
                  issued_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  idempotency_key TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_lease_one_active_per_job
                  ON leases(job_id) WHERE state='active';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_lease_idempotency
                  ON leases(idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS lease_resources(
                  resource TEXT PRIMARY KEY,
                  lease_id TEXT NOT NULL,
                  job_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fence_counters(
                  job_id TEXT PRIMARY KEY,
                  last_token INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id TEXT NOT NULL UNIQUE,
                  event_type TEXT NOT NULL,
                  job_id TEXT NOT NULL,
                  attempt_id TEXT,
                  lease_id TEXT,
                  fencing_token INTEGER,
                  node_id TEXT,
                  payload TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_job ON ledger(job_id, seq);
                """
            )
        finally:
            conn.close()
        self.path.chmod(0o600)

    # -- ledger -------------------------------------------------------------
    def append_event(
        self,
        db: sqlite3.Connection,
        event_type: str,
        *,
        job_id: str,
        payload: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        lease_id: str | None = None,
        fencing_token: int | None = None,
        node_id: str | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise LeaseError(f"unknown event type: {event_type}")
        eid = event_id or uuid.uuid4().hex
        created = created_at or _iso(utcnow())
        # Replay-safe: an event with an already-seen id is a no-op, not an error.
        existing = db.execute("SELECT * FROM ledger WHERE event_id=?", (eid,)).fetchone()
        if existing:
            return dict(existing)
        cur = db.execute(
            "INSERT INTO ledger(event_id,event_type,job_id,attempt_id,lease_id,fencing_token,node_id,payload,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (eid, event_type, job_id, attempt_id, lease_id, fencing_token, node_id, json.dumps(payload or {}), created),
        )
        return {
            "seq": cur.lastrowid,
            "event_id": eid,
            "event_type": event_type,
            "job_id": job_id,
            "payload": payload or {},
            "created_at": created,
        }

    def record(self, event_type: str, *, job_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append a standalone lifecycle event (e.g. BLOCKED) transactionally."""
        with self._write() as db:
            return self.append_event(db, event_type, job_id=job_id, payload=payload)

    def ledger_gaps(self) -> list[int]:
        """Return missing sequence numbers in the append-only ledger.

        A gap means the ledger was truncated/tampered. A reducer must flag this
        rather than silently reconstructing state across the hole.
        """
        db = self.connect()
        try:
            seqs = [r["seq"] for r in db.execute("SELECT seq FROM ledger ORDER BY seq").fetchall()]
        finally:
            db.close()
        if not seqs:
            return []
        expected = set(range(seqs[0], seqs[-1] + 1))
        return sorted(expected - set(seqs))

    def events(self, after_seq: int = 0) -> list[dict[str, Any]]:
        db = self.connect()
        try:
            rows = db.execute("SELECT * FROM ledger WHERE seq>? ORDER BY seq", (after_seq,)).fetchall()
        finally:
            db.close()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    # -- registration -------------------------------------------------------
    def register_job(self, job_id: str, *, work_key: str | None = None, event_id: str | None = None) -> None:
        """Idempotently announce a job. Replays of the same event_id are no-ops."""
        with self._write() as db:
            self.append_event(db, "JOB_CREATED", job_id=job_id, payload={"work_key": work_key}, event_id=event_id)

    # -- lease grant (compare-and-set) -------------------------------------
    def grant(
        self,
        job_id: str,
        node_id: str,
        *,
        execution_class: str = "AUTO",
        ttl_seconds: int = 300,
        resources: Iterable[str] = (),
        attempt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Lease:
        """Atomically acquire an exclusive lease, bumping the job's fence token.

        Raises LeaseConflict if another active lease owns the job or any
        requested resource. Replaying the same idempotency_key returns the
        original lease without allocating a new fence (replay-safe).
        """
        resources = tuple(sorted(set(resources)))
        now = utcnow()
        with self._write() as db:
            if idempotency_key:
                prior = db.execute(
                    "SELECT * FROM leases WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if prior:
                    self.append_event(
                        db,
                        "IDEMPOTENT_REPLAY",
                        job_id=prior["job_id"],
                        lease_id=prior["lease_id"],
                        fencing_token=prior["fencing_token"],
                        node_id=node_id,
                        payload={"idempotency_key": idempotency_key},
                    )
                    return Lease.from_row(prior)

            active = db.execute(
                "SELECT * FROM leases WHERE job_id=? AND state='active'", (job_id,)
            ).fetchone()
            if active is not None:
                lease = Lease.from_row(active)
                if lease.expired(now):
                    self._expire_row(db, active, now)
                else:
                    raise LeaseConflict(
                        f"job {job_id} already leased by {lease.node_id} (fence {lease.fencing_token})"
                    )

            for resource in resources:
                holder = db.execute(
                    "SELECT * FROM lease_resources WHERE resource=?", (resource,)
                ).fetchone()
                if holder is not None:
                    raise LeaseConflict(
                        f"resource {resource} held by lease {holder['lease_id']} (job {holder['job_id']})"
                    )

            db.execute(
                "INSERT INTO fence_counters(job_id,last_token) VALUES(?,1)"
                " ON CONFLICT(job_id) DO UPDATE SET last_token=last_token+1",
                (job_id,),
            )
            token = db.execute(
                "SELECT last_token FROM fence_counters WHERE job_id=?", (job_id,)
            ).fetchone()["last_token"]

            lease = Lease(
                lease_id=uuid.uuid4().hex,
                job_id=job_id,
                attempt_id=attempt_id or uuid.uuid4().hex,
                node_id=node_id,
                fencing_token=int(token),
                resources=resources,
                execution_class=execution_class,
                state=LEASE_ACTIVE,
                issued_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
                idempotency_key=idempotency_key,
            )
            db.execute(
                "INSERT INTO leases(lease_id,job_id,attempt_id,node_id,fencing_token,resources,"
                "execution_class,state,issued_at,expires_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lease.lease_id,
                    lease.job_id,
                    lease.attempt_id,
                    lease.node_id,
                    lease.fencing_token,
                    json.dumps(list(lease.resources)),
                    lease.execution_class,
                    lease.state,
                    lease.issued_at,
                    lease.expires_at,
                    lease.idempotency_key,
                ),
            )
            for resource in resources:
                db.execute(
                    "INSERT INTO lease_resources(resource,lease_id,job_id) VALUES(?,?,?)",
                    (resource, lease.lease_id, job_id),
                )
            self.append_event(
                db,
                "LEASE_GRANTED",
                job_id=job_id,
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
                fencing_token=lease.fencing_token,
                node_id=node_id,
                payload={
                    "execution_class": execution_class,
                    "resources": list(resources),
                    "ttl_seconds": ttl_seconds,
                },
            )
            return lease

    # -- renew --------------------------------------------------------------
    def renew(self, job_id: str, lease_id: str, fencing_token: int, *, ttl_seconds: int = 300) -> Lease:
        """Extend an active lease. Renewals never change the fence token."""
        now = utcnow()
        with self._write() as db:
            row = db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                raise StaleWorker(f"unknown lease {lease_id}")
            lease = Lease.from_row(row)
            if (
                lease.job_id != job_id
                or lease.state != LEASE_ACTIVE
                or lease.fencing_token != fencing_token
                or not self._is_current_fence(db, job_id, fencing_token)
                or lease.expired(now)
            ):
                raise StaleWorker(
                    f"renew rejected: lease {lease_id} fence {fencing_token} is not the current active owner of {job_id}"
                )
            new_expiry = _iso(now + timedelta(seconds=ttl_seconds))
            db.execute("UPDATE leases SET expires_at=? WHERE lease_id=?", (new_expiry, lease_id))
            self.append_event(
                db,
                "LEASE_RENEWED",
                job_id=job_id,
                attempt_id=lease.attempt_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                node_id=lease.node_id,
                payload={"expires_at": new_expiry},
            )
            return Lease.from_row(db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone())

    # -- protected boundaries ----------------------------------------------
    def commit_side_effect(
        self, job_id: str, lease_id: str, fencing_token: int, *, action: str
    ) -> dict[str, Any]:
        """A protected side effect may only be committed by the current fence owner."""
        with self._write() as db:
            lease = self._require_current(db, job_id, lease_id, fencing_token)
            return self.append_event(
                db,
                "SIDE_EFFECT_COMMITTED",
                job_id=job_id,
                attempt_id=lease["attempt_id"],
                lease_id=lease_id,
                fencing_token=fencing_token,
                node_id=lease["node_id"],
                payload={"action": action},
            )

    def submit_result(
        self,
        job_id: str,
        lease_id: str,
        fencing_token: int,
        *,
        final_status: str,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit a terminal result. Stale or expired owners are rejected."""
        terminal_map = {
            "completed": "COMPLETED",
            "failed": "FAILED",
            "blocked": "BLOCKED",
            "cancelled": "CANCELLED",
        }
        if final_status not in terminal_map:
            raise LeaseError(f"unsupported final_status: {final_status}")
        with self._write() as db:
            lease = self._require_current(db, job_id, lease_id, fencing_token)
            self.append_event(
                db,
                "RESULT_SUBMITTED",
                job_id=job_id,
                attempt_id=lease["attempt_id"],
                lease_id=lease_id,
                fencing_token=fencing_token,
                node_id=lease["node_id"],
                payload={"final_status": final_status, "evidence": evidence or {}},
            )
            new_state = LEASE_COMPLETED if final_status == "completed" else LEASE_FAILED
            db.execute("UPDATE leases SET state=? WHERE lease_id=?", (new_state, lease_id))
            self._free_resources(db, lease_id)
            event = self.append_event(
                db,
                terminal_map[final_status],
                job_id=job_id,
                attempt_id=lease["attempt_id"],
                lease_id=lease_id,
                fencing_token=fencing_token,
                node_id=lease["node_id"],
                payload={"evidence": evidence or {}},
            )
            return event

    def release(self, job_id: str, lease_id: str, fencing_token: int) -> None:
        with self._write() as db:
            lease = self._require_current(db, job_id, lease_id, fencing_token)
            db.execute("UPDATE leases SET state=? WHERE lease_id=?", (LEASE_RELEASED, lease_id))
            self._free_resources(db, lease_id)
            self.append_event(
                db,
                "LEASE_RELEASED",
                job_id=job_id,
                attempt_id=lease["attempt_id"],
                lease_id=lease_id,
                fencing_token=fencing_token,
                node_id=lease["node_id"],
            )

    # -- expiry / recovery --------------------------------------------------
    def expire_due(self, now: datetime | None = None) -> list[str]:
        """Expire active leases past their deadline. Returns expired lease ids."""
        now = now or utcnow()
        expired: list[str] = []
        with self._write() as db:
            rows = db.execute("SELECT * FROM leases WHERE state='active'").fetchall()
            for row in rows:
                if Lease.from_row(row).expired(now):
                    self._expire_row(db, row, now)
                    expired.append(row["lease_id"])
        return expired

    def _expire_row(self, db: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> None:
        db.execute("UPDATE leases SET state=? WHERE lease_id=?", (LEASE_EXPIRED, row["lease_id"]))
        self._free_resources(db, row["lease_id"])
        self.append_event(
            db,
            "LEASE_EXPIRED",
            job_id=row["job_id"],
            attempt_id=row["attempt_id"],
            lease_id=row["lease_id"],
            fencing_token=row["fencing_token"],
            node_id=row["node_id"],
            payload={"expired_at": _iso(now)},
        )

    def _free_resources(self, db: sqlite3.Connection, lease_id: str) -> None:
        db.execute("DELETE FROM lease_resources WHERE lease_id=?", (lease_id,))

    def _is_current_fence(self, db: sqlite3.Connection, job_id: str, fencing_token: int) -> bool:
        row = db.execute("SELECT last_token FROM fence_counters WHERE job_id=?", (job_id,)).fetchone()
        return row is not None and int(row["last_token"]) == fencing_token

    def _require_current(
        self, db: sqlite3.Connection, job_id: str, lease_id: str, fencing_token: int
    ) -> sqlite3.Row:
        row = db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise StaleWorker(f"unknown lease {lease_id}")
        lease = Lease.from_row(row)
        if (
            lease.job_id != job_id
            or lease.state != LEASE_ACTIVE
            or lease.fencing_token != fencing_token
            or not self._is_current_fence(db, job_id, fencing_token)
            or lease.expired()
        ):
            raise StaleWorker(
                f"stale worker: lease {lease_id} fence {fencing_token} is not the current active owner of {job_id}"
            )
        return row

    # -- reducer ------------------------------------------------------------
    def reduce(self, after_seq: int = 0) -> ReducedState:
        """Deterministically fold the append-only ledger into current state."""
        state = ReducedState()
        for event in self.events(after_seq):
            self._apply(state, event)
        return state

    @staticmethod
    def _apply(state: ReducedState, event: dict[str, Any]) -> None:
        job_id = event["job_id"]
        job = state.jobs.setdefault(
            job_id,
            {
                "state": "created",
                "attempts": [],
                "current_lease_id": None,
                "current_fence": 0,
                "owner": None,
            },
        )
        etype = event["event_type"]
        fence = event.get("fencing_token")
        if etype == "JOB_CREATED":
            job["state"] = "eligible"
        elif etype == "JOB_ELIGIBLE":
            job["state"] = "eligible"
        elif etype == "LEASE_GRANTED":
            job["state"] = "running"
            job["current_lease_id"] = event["lease_id"]
            job["current_fence"] = fence
            job["owner"] = event.get("node_id")
            if event.get("attempt_id") and event["attempt_id"] not in job["attempts"]:
                job["attempts"].append(event["attempt_id"])
        elif etype == "IDEMPOTENT_REPLAY":
            pass  # observation only; ownership/fence unchanged
        elif etype in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "SUPERSEDED"):
            job["state"] = etype.lower()
            job["current_lease_id"] = None
            job["owner"] = None
        # LEASE_RENEWED / LEASE_EXPIRED / LEASE_RELEASED / ATTEMPT_EVENT /
        # SIDE_EFFECT_COMMITTED / RESULT_* do not change ownership by themselves;
        # a LEASE_EXPIRED is followed by (or preceded by) a fresh LEASE_GRANTED
        # to express reassignment.
        elif etype == "LEASE_EXPIRED" and job["current_lease_id"] == event.get("lease_id"):
            job["state"] = "eligible"
            job["current_lease_id"] = None
            job["owner"] = None

    def reconstruct_from_ledger(self) -> None:
        """Disaster recovery: rebuild leases/fences/resources purely from ledger.

        Used after coordinator restart when the hot tables are missing or suspect.
        The ledger remains the immutable source of truth.
        """
        state = self.reduce()
        with self._write() as db:
            db.execute("DELETE FROM lease_resources")
            db.execute("UPDATE leases SET state='expired' WHERE state='active'")
            db.execute("DELETE FROM fence_counters")
            for job_id, job in state.jobs.items():
                if job["current_fence"]:
                    db.execute(
                        "INSERT INTO fence_counters(job_id,last_token) VALUES(?,?)",
                        (job_id, job["current_fence"]),
                    )
                if job["state"] == "running" and job["current_lease_id"]:
                    db.execute(
                        "UPDATE leases SET state='active' WHERE lease_id=?", (job["current_lease_id"],)
                    )
                    row = db.execute(
                        "SELECT resources FROM leases WHERE lease_id=?", (job["current_lease_id"],)
                    ).fetchone()
                    if row:
                        for resource in json.loads(row["resources"]):
                            db.execute(
                                "INSERT OR IGNORE INTO lease_resources(resource,lease_id,job_id) VALUES(?,?,?)",
                                (resource, job["current_lease_id"], job_id),
                            )

    # -- git compatibility view --------------------------------------------
    def to_git_view(self) -> dict[str, list[str]]:
        """Materialize the classic lifecycle directory view from the reducer."""
        view: dict[str, list[str]] = {
            "queue": [],
            "running": [],
            "completed": [],
            "blocked": [],
            "failed": [],
        }
        state_to_dir = {
            "created": "queue",
            "eligible": "queue",
            "running": "running",
            "completed": "completed",
            "blocked": "blocked",
            "cancelled": "blocked",
            "failed": "failed",
            "superseded": "completed",
        }
        for job_id, job in self.reduce().jobs.items():
            target = state_to_dir.get(job["state"], "queue")
            view[target].append(job_id)
        for key in view:
            view[key].sort()
        return view
