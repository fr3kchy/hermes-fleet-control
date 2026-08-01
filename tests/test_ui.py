from pathlib import Path
from fastapi.testclient import TestClient
from hermes_fleet.app import create_app
from hermes_fleet.config import Settings


def test_dashboard_uses_backend_state_and_has_no_fake_success(tmp_path: Path):
    client = TestClient(create_app(Settings(database_path=tmp_path / "x.db", enrollment_secret="strong-secret", operator_token="operator-token")))
    page = client.get("/ui").text
    assert "Fleet Overview" in page
    assert "fetch('/api/v1/fleet'" in page
    assert "HERMES_FLEET_OK" not in page
    assert "Something went wrong" not in page
