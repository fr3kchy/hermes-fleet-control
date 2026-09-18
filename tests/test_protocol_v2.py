import json
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from hermes_fleet.app import create_app
from hermes_fleet.config import Settings
from hermes_fleet.security import sign_payload, sign_request


def setup(tmp_path: Path):
    client = TestClient(create_app(Settings(database_path=tmp_path / "fleet.db", enrollment_secret="test-secret", operator_token="operator-token", hermes_binary="/bin/echo")))
    token = client.post("/api/v1/enrollment/tokens", json={"node_name": "node"}, headers={"X-Enrollment-Secret": "test-secret"}).json()["token"]
    node = client.post("/api/v1/nodes/enroll", json={"token": token, "node_name": "node", "capabilities": {"os": "linux", "architecture": "x86_64", "profiles": ["engineer"], "tools": ["git"], "kanban": True}}).json()
    heartbeat = {"status": "online", "metrics": {"benchmark_score": 8}, "capabilities": {}}
    client.post(f"/api/v1/nodes/{node['node_id']}/heartbeat", json=heartbeat, headers={"X-Node-Signature": sign_payload(node["node_secret"], heartbeat)})
    client.headers["Authorization"] = "Bearer operator-token"
    return client, node


def signed(client, node, method, path, payload=None, nonce=None):
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
    timestamp, nonce = str(int(time.time())), nonce or str(uuid.uuid4())
    headers = {"X-Node-Timestamp": timestamp, "X-Node-Nonce": nonce, "X-Node-Signature": sign_request(node["node_secret"], method, path, timestamp, nonce, body)}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, content=body if payload is not None else None, headers=headers), headers


def test_signed_assignment_round_trip_and_evidence_gate(tmp_path: Path):
    client, node = setup(tmp_path)
    created = client.post("/api/v2/tasks", json={"protocol_version": "2.0", "destination_node_id": node["node_id"], "profile": "engineer", "objective": "Fix the bug", "acceptance_criteria": ["tests pass"], "requirements": {"os": "linux", "tools": ["git"]}}, headers={"Idempotency-Key": "global-1"})
    assert created.status_code == 202
    task = created.json()
    path = f"/api/v2/nodes/{node['node_id']}/assignments/next"
    assignment, headers = signed(client, node, "GET", path)
    assert assignment.status_code == 200
    assert assignment.json()["assignment"]["id"] == task["id"]
    replay = client.get(path, headers=headers)
    assert replay.status_code == 409
    redelivery, _ = signed(client, node, "GET", path)
    assert redelivery.json()["assignment"]["id"] == task["id"]
    ack_path = f"/api/v2/nodes/{node['node_id']}/assignments/{task['id']}/ack"
    ack = {"protocol_version": "2.0", "event_id": str(uuid.uuid4()), "timestamp": str(int(time.time())), "sequence": 1, "disposition": "accepted", "idempotency_key": "global-1", "remote_board_id": "default", "remote_card_id": "t_abc"}
    assert signed(client, node, "POST", ack_path, ack)[0].status_code == 200
    event_path = f"/api/v2/nodes/{node['node_id']}/events"
    done = {"protocol_version": "2.0", "event_id": str(uuid.uuid4()), "fleet_task_id": task["id"], "idempotency_key": "global-1", "timestamp": str(int(time.time())), "sequence": 2, "event_type": "done", "remote_card_id": "t_abc", "evidence": []}
    assert signed(client, node, "POST", event_path, done)[0].json()["task"]["status"] == "verifying"
    done["event_id"], done["sequence"], done["evidence"] = str(uuid.uuid4()), 3, [{"type": "test", "result": "passed"}]
    assert signed(client, node, "POST", event_path, done)[0].json()["task"]["status"] == "complete"


def test_route_rejection_exposes_reason_codes(tmp_path: Path):
    client, node = setup(tmp_path)
    response = client.post("/api/v2/tasks", json={"profile": "engineer", "objective": "GPU job", "acceptance_criteria": ["artifact exists"], "requirements": {"gpu": True}})
    assert response.status_code == 409
    candidates = response.json()["detail"]["route"]["candidates"]
    assert "GPU_UNAVAILABLE" in candidates[0]["reason_codes"]


def test_duplicate_scheduler_requests_share_one_assignment(tmp_path: Path):
    settings = Settings(database_path=tmp_path / "fleet.db", enrollment_secret="test-secret", operator_token="operator-token", hermes_binary="/bin/echo")
    first_app = create_app(settings)
    first = TestClient(first_app)
    second = TestClient(create_app(settings))
    token = first.post("/api/v1/enrollment/tokens", json={"node_name": "shared-node"}, headers={"X-Enrollment-Secret": "test-secret"}).json()["token"]
    node = first.post("/api/v1/nodes/enroll", json={"token": token, "node_name": "shared-node", "capabilities": {"os": "linux", "profiles": ["engineer"]}}).json()
    heartbeat = {"status": "online", "metrics": {}, "capabilities": {}}
    first.post(f"/api/v1/nodes/{node['node_id']}/heartbeat", json=heartbeat, headers={"X-Node-Signature": sign_payload(node["node_secret"], heartbeat)})
    payload = {"protocol_version": "2.0", "destination_node_id": node["node_id"], "profile": "engineer", "objective": "one assignment", "acceptance_criteria": ["evidence"]}
    first.headers["Authorization"] = second.headers["Authorization"] = "Bearer operator-token"
    left = first.post("/api/v2/tasks", json=payload, headers={"Idempotency-Key": "same-global-work"})
    right = second.post("/api/v2/tasks", json=payload, headers={"Idempotency-Key": "same-global-work"})
    assert left.status_code == right.status_code == 202
    assert left.json()["id"] == right.json()["id"]
    with first_app.state.db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM tasks WHERE idempotency_key='same-global-work'").fetchone()[0] == 1
