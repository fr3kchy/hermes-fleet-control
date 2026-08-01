from pathlib import Path


def test_systemd_units_are_independent_and_hardened():
    root = Path(__file__).parents[1]
    api = (root / "deploy/systemd/hermes-fleet-api.service").read_text()
    worker = (root / "deploy/systemd/hermes-fleet-worker.service").read_text()
    node = (root / "deploy/systemd/hermes-fleet-node.service").read_text()
    for unit in (api, worker, node):
        assert "Restart=on-failure" in unit
        assert "NoNewPrivileges=true" in unit
        assert "EnvironmentFile=%h/.config/hermes-fleet-control/env" in unit
    assert "uvicorn" in api
    assert "hermes_fleet.worker" in worker
