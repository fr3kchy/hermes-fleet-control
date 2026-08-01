import json
import os
import time
from pathlib import Path

import httpx

from .connector import capability_manifest
from .security import sign_payload


def heartbeat_once(base_url: str, state_path: Path) -> dict:
    credentials = json.loads(state_path.read_text())
    body = {
        "status": "online",
        "metrics": {"load_1m": os.getloadavg()[0]},
        "capabilities": {**capability_manifest(), "protocol_version": "1.0"},
    }
    response = httpx.post(
        f"{base_url}/api/v1/nodes/{credentials['node_id']}/heartbeat",
        json=body,
        headers={"X-Node-Signature": sign_payload(credentials["node_secret"], body)},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    base_url = os.getenv("HFC_BASE_URL", "http://127.0.0.1:8876")
    state_path = Path(os.getenv("HFC_NODE_STATE", "data/parrot-local.json"))
    interval = max(10, int(os.getenv("HFC_HEARTBEAT_SECONDS", "30")))
    delay = 1
    while True:
        try:
            heartbeat_once(base_url, state_path)
            delay = 1
            time.sleep(interval)
        except (OSError, ValueError, httpx.HTTPError):
            time.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    main()
