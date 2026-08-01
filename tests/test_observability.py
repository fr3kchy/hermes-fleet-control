from pathlib import Path
from fastapi.testclient import TestClient
from hermes_fleet.app import create_app
from hermes_fleet.config import Settings


def test_correlation_id_and_metrics_are_backend_confirmed(tmp_path: Path):
    client = TestClient(create_app(Settings(database_path=tmp_path / "x.db", enrollment_secret="strong-secret", operator_token="operator-token")))
    health = client.get("/healthz", headers={"X-Correlation-ID": "corr-123"})
    assert health.headers["X-Correlation-ID"] == "corr-123"
    metrics = client.get("/metrics", headers={"Authorization": "Bearer operator-token"})
    assert metrics.status_code == 200
    assert "hermes_fleet_nodes" in metrics.text
    assert "hermes_fleet_tasks" in metrics.text
