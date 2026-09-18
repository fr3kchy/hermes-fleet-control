import json
import os
import shutil
import time
import uuid
from pathlib import Path

import httpx

from .connector import capability_manifest
from .kanban_adapter import AssignmentMap, HermesKanbanAdapter, KanbanError
from .security import fresh_timestamp, sign_payload, signed_headers, verify_request_signature


def heartbeat_once(base_url: str, state_path: Path) -> dict:
    credentials = json.loads(state_path.read_text())
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = 0
    storage = shutil.disk_usage(state_path.parent)
    body = {"status": "online", "metrics": {"load_1m": load, "storage_free_gb": round(storage.free / 2**30, 2)}, "capabilities": capability_manifest()}
    response = httpx.post(f"{base_url}/api/v1/nodes/{credentials['node_id']}/heartbeat", json=body, headers={"X-Node-Signature": sign_payload(credentials["node_secret"], body)}, timeout=20)
    response.raise_for_status()
    return response.json()


def _signed_request(client: httpx.Client, credentials: dict, method: str, path: str, payload: dict | None = None, **kwargs) -> httpx.Response:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() if payload is not None else b""
    headers = signed_headers(credentials["node_secret"], method, path, body)
    response = client.request(method, path, content=body if payload is not None else None, headers={**headers, **({"Content-Type": "application/json"} if payload is not None else {})}, **kwargs)
    response.raise_for_status()
    timestamp = response.headers.get("X-Coordinator-Timestamp", "")
    nonce = response.headers.get("X-Coordinator-Nonce", "")
    signature = response.headers.get("X-Coordinator-Signature", "")
    if not fresh_timestamp(timestamp) or not verify_request_signature(credentials["node_secret"], "RESPONSE", path, timestamp, nonce, response.content, signature):
        raise RuntimeError("Coordinator response signature is invalid")
    return response


def dispatch_once(base_url: str, state_path: Path, map_path: Path, hermes_binary: str = "hermes", wait: int = 0) -> bool:
    credentials = json.loads(state_path.read_text())
    node_id = credentials["node_id"]
    mapping = AssignmentMap(map_path)
    adapter = HermesKanbanAdapter(hermes_binary)
    path = f"/api/v2/nodes/{node_id}/assignments/next"
    with httpx.Client(base_url=base_url, timeout=max(20, wait + 5)) as client:
        response = _signed_request(client, credentials, "GET", path, params={"wait": min(max(wait, 0), 20)})
        assignment = response.json().get("assignment")
        if not assignment:
            return False
        existing = mapping.get(assignment["id"])
        disposition, reason, detail = "duplicate", None, None
        if not existing:
            try:
                card = adapter.create(assignment)
                card_id = str(card.get("id") or card.get("task", {}).get("id") or card.get("card_id"))
                board_id = str(card.get("board") or card.get("board_id") or "default")
                if not card_id or card_id == "None":
                    raise KanbanError("Hermes Kanban response did not include a card id")
                existing = mapping.put(assignment["id"], assignment["idempotency_key"], board_id, card_id)
                disposition = "accepted"
            except (KanbanError, OSError) as exc:
                disposition, reason, detail = "rejected", "KANBAN_CREATE_FAILED", str(exc)[:2000]
        ack_path = f"/api/v2/nodes/{node_id}/assignments/{assignment['id']}/ack"
        ack = {"protocol_version": "2.0", "event_id": str(uuid.uuid4()), "timestamp": str(int(time.time())), "sequence": 1, "disposition": disposition, "idempotency_key": assignment["idempotency_key"], "remote_board_id": existing["board_id"] if existing else None, "remote_card_id": existing["card_id"] if existing else None, "reason_code": reason, "detail": detail}
        _signed_request(client, credentials, "POST", ack_path, ack)
        if existing:
            mapping.ensure_sequence(assignment["id"], 1)
        return disposition in {"accepted", "duplicate"}


def reconcile_assignments_once(base_url: str, state_path: Path, map_path: Path, hermes_binary: str = "hermes") -> int:
    credentials, mapping = json.loads(state_path.read_text()), AssignmentMap(map_path)
    adapter, sent = HermesKanbanAdapter(hermes_binary), 0
    status_map = {"todo": "created", "ready": "ready", "running": "running", "blocked": "blocked", "scheduled": "blocked", "review": "review", "done": "done", "failed": "failed", "cancelled": "cancelled"}
    with httpx.Client(base_url=base_url, timeout=30) as client:
        for record in mapping.all():
            try:
                card = adapter.show(record["card_id"])
            except (KanbanError, OSError):
                continue
            task = card.get("task", card)
            local_status = str(task.get("status", "unknown")).lower()
            event_type = status_map.get(local_status, "unknown")
            if record.get("last_status") == event_type:
                continue
            sequence = mapping.advance(record["fleet_task_id"])
            evidence = task.get("evidence") or task.get("verification_evidence") or []
            if not isinstance(evidence, list):
                evidence = [{"type": "local_kanban", "value": evidence}]
            event = {"protocol_version": "2.0", "event_id": str(uuid.uuid4()), "fleet_task_id": record["fleet_task_id"], "idempotency_key": record["idempotency_key"], "timestamp": str(int(time.time())), "sequence": sequence, "event_type": event_type, "remote_board_id": record["board_id"], "remote_card_id": record["card_id"], "result_summary": task.get("result") or task.get("summary"), "evidence": evidence, "artifacts": task.get("artifacts") or [], "failure_code": task.get("failure_code")}
            path = f"/api/v2/nodes/{credentials['node_id']}/events"
            _signed_request(client, credentials, "POST", path, event)
            mapping.set_status(record["fleet_task_id"], event_type)
            sent += 1
    return sent


def main() -> None:
    base_url = os.getenv("HFC_BASE_URL", "http://127.0.0.1:8876")
    state_path = Path(os.getenv("HFC_NODE_STATE", "data/parrot-local.json"))
    map_path = Path(os.getenv("HFC_ASSIGNMENT_MAP", "data/node-assignments.db"))
    interval = max(10, int(os.getenv("HFC_HEARTBEAT_SECONDS", "30")))
    hermes_binary = os.getenv("HFC_HERMES_BINARY", "hermes")
    last_heartbeat, last_reconcile, delay = 0.0, 0.0, 1
    while True:
        try:
            if time.monotonic() - last_heartbeat >= interval:
                heartbeat_once(base_url, state_path)
                last_heartbeat = time.monotonic()
            dispatch_once(base_url, state_path, map_path, hermes_binary, wait=min(interval, 20))
            if time.monotonic() - last_reconcile >= 900:
                reconcile_assignments_once(base_url, state_path, map_path, hermes_binary)
                last_reconcile = time.monotonic()
            delay = 1
        except (OSError, ValueError, RuntimeError, httpx.HTTPError):
            time.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    main()
