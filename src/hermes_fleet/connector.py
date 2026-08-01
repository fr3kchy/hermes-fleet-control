import os
import platform
from pathlib import Path

import httpx

from .security import sign_payload


def capability_manifest() -> dict:
    return {
        "os": platform.system().lower(),
        "hostname": platform.node(),
        "architecture": platform.machine(),
        "cpu_count": os.cpu_count(),
        "profiles": ["default"],
        "connector_version": "0.1.0",
    }


def enroll_and_heartbeat(base_url: str, token: str, node_name: str, state_path: Path) -> dict:
    with httpx.Client(base_url=base_url, timeout=20) as client:
        enrolled = client.post("/api/v1/nodes/enroll", json={"token": token, "node_name": node_name, "capabilities": capability_manifest()})
        enrolled.raise_for_status()
        credentials = enrolled.json()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(__import__("json").dumps(credentials))
        state_path.chmod(0o600)
        body = {"status": "online", "metrics": {}, "capabilities": capability_manifest()}
        heartbeat = client.post(f"/api/v1/nodes/{credentials['node_id']}/heartbeat", json=body, headers={"X-Node-Signature": sign_payload(credentials["node_secret"], body)})
        heartbeat.raise_for_status()
        return {"enrollment": credentials, "heartbeat": heartbeat.json()}
