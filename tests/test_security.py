from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from hermes_fleet.app import create_app
from hermes_fleet.config import Settings


def test_production_rejects_default_secrets(tmp_path: Path):
    with pytest.raises(ValueError, match="HFC_ENROLLMENT_SECRET"):
        Settings(database_path=tmp_path / "x.db", environment="production", enrollment_secret="change-me-local-only", operator_token="change-me-operator")


def test_operator_endpoints_require_bearer_token(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "fleet.db",
        enrollment_secret="enrollment-secret-strong",
        operator_token="operator-token-strong",
        hermes_binary="/bin/echo",
    )
    client = TestClient(create_app(settings))
    assert client.get("/api/v1/fleet").status_code == 401
    assert client.get("/api/v1/events").status_code == 401
    assert client.post("/api/v1/tasks", json={"node_id": "missing", "profile": "default", "prompt": "x"}).status_code == 401
    assert client.get("/api/v1/fleet", headers={"Authorization": "Bearer operator-token-strong"}).status_code == 200
    with pytest.raises(Exception):
        with client.websocket_connect("/api/v1/events/ws"):
            pass
