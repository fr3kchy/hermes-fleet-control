import json
from pathlib import Path

from fastapi.testclient import TestClient

from hermes_fleet.app import create_app
from hermes_fleet.config import Settings


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(database_path=tmp_path / "fleet.db", enrollment_secret="test-secret", operator_token="operator-token", hermes_binary="/bin/echo")
    client = TestClient(create_app(settings))
    client.headers["Authorization"] = "Bearer operator-token"
    return client


def enroll(client: TestClient) -> dict:
    assert client.post("/api/v1/enrollment/tokens", json={"node_name": "local-node"}).status_code == 401
    response = client.post("/api/v1/enrollment/tokens", json={"node_name": "local-node"}, headers={"X-Enrollment-Secret": "test-secret"})
    assert response.status_code == 201
    token = response.json()["token"]
    response = client.post("/api/v1/nodes/enroll", json={
        "token": token,
        "node_name": "local-node",
        "capabilities": {"os": "linux", "cpu_count": 8, "profiles": ["default"]},
    })
    assert response.status_code == 201
    return response.json()


def test_secure_enrollment_is_single_use_and_fleet_is_durable(tmp_path: Path):
    client = make_client(tmp_path)
    enrolled = enroll(client)
    assert enrolled["node_id"]
    assert enrolled["node_secret"]
    token = client.post("/api/v1/enrollment/tokens", json={"node_name": "other"}, headers={"X-Enrollment-Secret": "test-secret"}).json()["token"]
    payload = {"token": token, "node_name": "wrong-name", "capabilities": {}}
    assert client.post("/api/v1/nodes/enroll", json=payload).status_code == 403
    assert len(client.get("/api/v1/fleet").json()["nodes"]) == 1


def test_signed_heartbeat_updates_node_and_rejects_bad_signature(tmp_path: Path):
    client = make_client(tmp_path)
    enrolled = enroll(client)
    body = {"status": "online", "metrics": {"cpu_percent": 12.5}, "capabilities": {"gpu": "RTX 3060"}}
    bad = client.post(f"/api/v1/nodes/{enrolled['node_id']}/heartbeat", json=body, headers={"X-Node-Signature": "bad"})
    assert bad.status_code == 401
    from hermes_fleet.security import sign_payload
    signature = sign_payload(enrolled["node_secret"], body)
    good = client.post(f"/api/v1/nodes/{enrolled['node_id']}/heartbeat", json=body, headers={"X-Node-Signature": signature})
    assert good.status_code == 200
    node = client.get("/api/v1/fleet").json()["nodes"][0]
    assert node["status"] == "online"
    assert node["capabilities"]["gpu"] == "RTX 3060"
    partial = {"status": "online", "metrics": {"cpu_percent": 9.0}, "capabilities": {"connector_version": "0.2"}}
    assert client.post(f"/api/v1/nodes/{enrolled['node_id']}/heartbeat", json=partial, headers={"X-Node-Signature": sign_payload(enrolled["node_secret"], partial)}).status_code == 200
    merged = client.get("/api/v1/fleet").json()["nodes"][0]["capabilities"]
    assert merged["gpu"] == "RTX 3060"
    assert merged["connector_version"] == "0.2"


def test_task_dispatch_is_durable_and_persists_events(tmp_path: Path):
    client = make_client(tmp_path)
    enrolled = enroll(client)
    response = client.post("/api/v1/tasks", json={"node_id": enrolled["node_id"], "profile": "default", "prompt": "Return only: HERMES_FLEET_OK"})
    assert response.status_code == 202
    task = response.json()
    assert task["status"] == "queued"
    events = client.get("/api/v1/events").json()["events"]
    assert {e["type"] for e in events} >= {"node.enrolled", "task.queued"}


def test_health_exposes_backend_confirmation(tmp_path: Path):
    client = make_client(tmp_path)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
