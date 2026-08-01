from pathlib import Path
from fastapi.testclient import TestClient

from hermes_fleet.app import create_app
from hermes_fleet.config import Settings
from hermes_fleet.worker import Worker


def enroll(client: TestClient) -> dict:
    token = client.post("/api/v1/enrollment/tokens", json={"node_name": "local-node"}, headers={"X-Enrollment-Secret": "test-secret"}).json()["token"]
    return client.post("/api/v1/nodes/enroll", json={"token": token, "node_name": "local-node", "capabilities": {"profiles": ["default"]}}).json()


def test_task_is_durable_before_worker_confirms_success(tmp_path: Path):
    settings = Settings(database_path=tmp_path / "fleet.db", enrollment_secret="test-secret", operator_token="operator-token", hermes_binary="/bin/echo")
    client = TestClient(create_app(settings))
    client.headers["Authorization"] = "Bearer operator-token"
    node = enroll(client)
    response = client.post("/api/v1/tasks", json={"node_id": node["node_id"], "profile": "default", "prompt": "QUEUE_OK"}, headers={"Idempotency-Key": "task-1"})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    duplicate = client.post("/api/v1/tasks", json={"node_id": node["node_id"], "profile": "default", "prompt": "QUEUE_OK"}, headers={"Idempotency-Key": "task-1"})
    assert duplicate.json()["id"] == response.json()["id"]
    worker = Worker(client.app.state.db, settings)
    assert worker.run_once() is True
    task = client.get(f"/api/v1/tasks/{response.json()['id']}").json()
    assert task["status"] == "succeeded"
    assert "QUEUE_OK" in task["output"]
